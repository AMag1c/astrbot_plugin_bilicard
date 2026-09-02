"""B站数据抓取层：视频信息、在线人数、热门评论、字幕、UP主投稿等接口封装。

所有方法在失败时返回 None / 空列表，并记录日志，不向上抛异常。
"""

import asyncio
import base64
import re
import time
from typing import List, Optional, Tuple

import aiohttp

from . import wbi
from .log import logger

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_ONLINE = "https://api.bilibili.com/x/player/online/total"
API_REPLY = "https://api.bilibili.com/x/v2/reply"
API_SUBTITLE = "https://api.bilibili.com/x/player/wbi/v2"

_BV_OR_AV_RE = re.compile(r"(BV[0-9A-Za-z]{10})|av(\d{1,12})", re.IGNORECASE)

# 直连被风控后的冷却时长（秒）：期间走库，过后自动重试直连
_DIRECT_COOLDOWN = 1800


class BiliClient:
    def __init__(self, cookies: Optional[dict] = None, timeout: int = 15):
        # cookies: {"SESSDATA": "...", "bili_jct": "..."}
        self.cookies = {k: v for k, v in (cookies or {}).items() if v}
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._buvid_ready = False
        self._blocked_at = 0.0  # 直连被 412 的时刻；冷却期内直接走库，过后再试

    def _headers(self, referer: str = "https://www.bilibili.com") -> dict:
        """B站风控会核对请求头是否像浏览器，少了 Origin / Sec-Fetch-* 更易被 412。"""
        h = {
            "User-Agent": _UA,
            "Referer": referer,
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return h

    async def _get_json(
        self,
        url: str,
        params: Optional[dict] = None,
        referer: str = "https://www.bilibili.com",
    ) -> Optional[dict]:
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(
                    url, params=params, headers=self._headers(referer)
                ) as resp:
                    if resp.status != 200:
                        if resp.status == 412:
                            # 风控常与设备标识失效有关：作废缓存，下次重新申请。
                            # 同时记下"直连走不通"，之后直接用库，省掉每次白挨一次
                            # 412——反复触发只会让风控更严
                            self._buvid_ready = False
                            self.cookies.pop("buvid3", None)
                            self.cookies.pop("buvid4", None)
                            self._blocked_at = time.time()
                            logger.warning(
                                "被 B站风控拦截(412) %s：%s 分钟内改走 bilibili-api 库，"
                                "之后自动重试直连。频繁触发多为请求过密或 IP 被盯上",
                                url,
                                _DIRECT_COOLDOWN // 60,
                            )
                        else:
                            logger.warning("请求 %s 返回 HTTP %s", url, resp.status)
                        return None
                    return await resp.json()
        except Exception as e:  # noqa: BLE001
            logger.error("请求 %s 失败: %s", url, e)
            return None

    async def _ensure_buvid(self) -> None:
        """获取 buvid3 / buvid4 写入 Cookie。

        B站风控要求携带设备标识：缺了它 wbi/space 接口报 -352，view 接口直接
        HTTP 412。接口返回值自带 infoc 后缀，原样放进 Cookie 即可。
        """
        if self._buvid_ready or self.cookies.get("buvid3"):
            self._buvid_ready = True
            return
        data = await self._get_json("https://api.bilibili.com/x/frontend/finger/spi")
        d = ((data or {}).get("data")) or {}
        if d.get("b_3"):
            self.cookies["buvid3"] = d["b_3"]
        if d.get("b_4"):  # 只给 buvid3 有时仍被拦，补上 buvid4 更稳
            self.cookies["buvid4"] = d["b_4"]
        self._buvid_ready = True

    async def resolve_b23(self, short_url: str) -> Optional[Tuple[str, str]]:
        """跟随 b23 短链重定向，提取 ('bv', bvid) 或 ('av', aid)。"""
        if not short_url.startswith("http"):
            short_url = "https://" + short_url
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                url = short_url
                for _ in range(10):  # 最多跟 10 跳
                    async with session.get(
                        url, headers=self._headers(), allow_redirects=False
                    ) as resp:
                        loc = resp.headers.get("Location")
                        if not loc:
                            break
                        url = loc
                        m = _BV_OR_AV_RE.search(url)
                        if m:
                            if m.group(1):
                                return ("bv", m.group(1))
                            return ("av", m.group(2))
        except Exception as e:  # noqa: BLE001
            logger.error("解析 b23 短链失败 %s: %s", short_url, e)
        return None

    async def get_video_info(
        self, bvid: Optional[str] = None, aid: Optional[str] = None
    ) -> Optional[dict]:
        """获取并标准化视频信息。bvid 与 aid 至少提供一个。"""
        params = {}
        if bvid:
            params["bvid"] = bvid
        elif aid:
            params["aid"] = aid
        else:
            return None

        d = None
        if time.time() - self._blocked_at > _DIRECT_COOLDOWN:
            # 先拿设备标识：view 接口现在也上了风控，不带 buvid 会直接 412
            await self._ensure_buvid()
            data = await self._get_json(API_VIEW, params)
            d = (data or {}).get("data") if (data or {}).get("code") == 0 else None
        if not d:
            # 直连被风控挡下时回退到库：它内部有设备指纹激活与签名处理，
            # UP主投稿接口早就因为同样的原因改走库了
            d = await self._video_info_via_lib(bvid, aid)
        if not d:
            logger.warning("视频信息获取失败: %s", (data or {}).get("message"))
            return None
        return self._normalize_video(d)

    async def _video_info_via_lib(self, bvid, aid) -> Optional[dict]:
        """用 bilibili-api 库取视频信息（免登录也可用）。失败返回 None。"""
        try:
            from bilibili_api import video

            cred = self._credential()  # 没登录就是 None，库会走匿名
            if bvid:
                v = video.Video(bvid=bvid, credential=cred)
            else:
                v = video.Video(aid=int(aid), credential=cred)
            # 切换到库这件事，412 那条 warning 已经说过，这里不重复刷屏
            logger.debug("[BiliCard] 已用 bilibili-api 库取视频信息")
            return await v.get_info() or None
        except Exception as e:  # noqa: BLE001
            logger.warning("库获取视频信息也失败: %s", e)
            return None

    @staticmethod
    def _normalize_video(d: dict) -> dict:
        """把 view 接口的原始数据整理成插件内部结构。"""
        owner = d.get("owner", {}) or {}
        stat = d.get("stat", {}) or {}
        return {
            "bvid": d.get("bvid", ""),
            "aid": d.get("aid", 0),
            "cid": d.get("cid", 0),
            "title": d.get("title", ""),
            "cover": d.get("pic", ""),
            "desc": d.get("desc", "") or "",
            "duration": d.get("duration", 0),  # 秒
            "pubdate": d.get("pubdate", 0),  # unix 时间戳
            "owner": {
                "name": owner.get("name", ""),
                "mid": owner.get("mid", 0),
                "face": owner.get("face", ""),
            },
            "stat": {
                "view": stat.get("view", 0),
                "danmaku": stat.get("danmaku", 0),
                "like": stat.get("like", 0),
                "coin": stat.get("coin", 0),
                "favorite": stat.get("favorite", 0),
                "share": stat.get("share", 0),
                "reply": stat.get("reply", 0),
            },
        }

    async def get_online(self, bvid: str, cid: int) -> Optional[str]:
        """视频实时在线观看人数（total，字符串）。"""
        data = await self._get_json(API_ONLINE, {"bvid": bvid, "cid": cid})
        if not data or data.get("code") != 0:
            return None
        return (data.get("data") or {}).get("total")

    async def get_hot_comments(self, aid: int, count: int = 3) -> List[dict]:
        """热门评论（sort=2 按热度），返回 [{name, message, like, avatar}]。"""
        data = await self._get_json(
            API_REPLY, {"type": 1, "oid": aid, "sort": 2, "pn": 1, "ps": max(count, 3)}
        )
        if not data or data.get("code") != 0:
            return []
        d = data.get("data") or {}
        # 优先用置顶热评 hots，不足再用 replies（已按热度排序）
        source = d.get("hots") or d.get("replies") or []
        result = []
        for r in source[:count]:
            member = r.get("member", {}) or {}
            content = r.get("content", {}) or {}
            result.append(
                {
                    "name": member.get("uname", ""),
                    "avatar": member.get("avatar", ""),
                    "message": (content.get("message", "") or "").strip(),
                    "like": r.get("like", 0),
                }
            )
        return result

    async def get_subtitle_text(
        self, bvid: str, cid: int, max_len: int = 4000
    ) -> Optional[str]:
        """获取视频字幕纯文本。先用自有 wbi 实现，拿不到再回退 bilibili-api 库。"""
        if not self.cookies.get("SESSDATA"):
            logger.debug("未配置 SESSDATA，跳过字幕获取")
            return None
        subtitles = await self._subtitle_list_wbi(bvid, cid)
        if not subtitles:
            logger.debug("wbi 未取到字幕，回退 bilibili-api 库")
            subtitles = await self._subtitle_list_lib(bvid, cid)
        if not subtitles:
            return None
        return await self._download_subtitle(subtitles, max_len)

    async def _subtitle_list_wbi(self, bvid: str, cid: int) -> list:
        """方式一：自有 wbi 签名拿字幕列表。"""
        try:
            await self._ensure_buvid()
            params = await wbi.sign_params({"bvid": bvid, "cid": cid}, self.cookies)
            data = await self._get_json(API_SUBTITLE, params)
            if not data or data.get("code") != 0:
                return []
            return ((data.get("data") or {}).get("subtitle") or {}).get(
                "subtitles"
            ) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("wbi 字幕列表获取失败: %s", e)
            return []

    async def _subtitle_list_lib(self, bvid: str, cid: int) -> list:
        """方式二：回退到 bilibili-api 库拿字幕列表。"""
        cred = self._credential()
        if not cred:
            return []
        try:
            from bilibili_api import video

            info = await video.Video(bvid, credential=cred).get_subtitle(cid)
            return (info or {}).get("subtitles") or []
        except Exception as e:  # noqa: BLE001
            logger.warning("库字幕列表获取失败: %s", e)
            return []

    async def _download_subtitle(self, subtitles: list, max_len: int) -> Optional[str]:
        """从字幕列表选中文优先，下载并拼接为纯文本。"""
        target = None
        for sub in subtitles:
            if str(sub.get("lan", "")).startswith("zh"):
                target = sub
                break
        target = target or subtitles[0]
        sub_url = target.get("subtitle_url", "")
        if not sub_url:
            return None
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url
        sub_data = await self._get_json(sub_url)
        if not sub_data:
            return None
        body = sub_data.get("body", []) or []
        text = "".join(item.get("content", "") for item in body)
        if not text:
            return None
        if len(text) > max_len:
            text = text[:max_len] + "…(字幕过长已截断)"
        return text

    async def fetch_image_data_uri(self, url: str) -> Optional[str]:
        """下载图片并转为 data URI，规避 B站图片的 Referer 防盗链。

        失败时返回原始 URL（降级，至少有机会被渲染服务直接加载）。
        """
        if not url:
            return url
        if url.startswith("//"):
            url = "https:" + url
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, headers=self._headers()) as resp:
                    if resp.status != 200:
                        logger.warning("图片下载返回 HTTP %s: %s", resp.status, url)
                        return url
                    ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                    raw = await resp.read()
            b64 = base64.b64encode(raw).decode()
            return f"data:{ctype};base64,{b64}"
        except Exception as e:  # noqa: BLE001
            logger.warning("图片下载失败 %s: %s", url, e)
            return url

    # ------------------------------------------------------------------ #
    # UP主相关（订阅功能使用，需 WBI 签名 + 登录 Cookie，否则会被风控）
    # ------------------------------------------------------------------ #
    def _credential(self):
        """构造 bilibili-api 凭证（无 SESSDATA 或库不可用时返回 None）。"""
        if not self.cookies.get("SESSDATA"):
            return None
        try:
            from bilibili_api import Credential

            return Credential(
                sessdata=self.cookies.get("SESSDATA", ""),
                bili_jct=self.cookies.get("bili_jct", ""),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("bilibili-api 库不可用: %s", e)
            return None

    async def refresh_cookies(self, ac_time_value: str) -> dict:
        """用 ac_time_value(refresh_token) 检查并刷新登录 Cookie（B站官方刷新机制，
        借 bilibili-api 实现，避免手写 RSA/correspond 流程）。

        返回 ``{"status": ...}``：
        - ``noop``      当前 Cookie 尚不需要刷新（B站每日才需刷新一次）；
        - ``refreshed`` 已刷新，附 ``cookies={SESSDATA, bili_jct, ac_time_value}``；
        - ``expired``   登录态已失效、无法刷新（需重新 /B站登录）；
        - ``unavailable`` 缺 ac_time_value / SESSDATA，或 bilibili-api 不可用。
        """
        if not ac_time_value or not self.cookies.get("SESSDATA"):
            return {"status": "unavailable"}
        try:
            from bilibili_api import Credential
        except Exception as e:  # noqa: BLE001
            logger.error("bilibili-api 库不可用，无法自动续期: %s", e)
            return {"status": "unavailable"}
        try:
            await self._ensure_buvid()  # 刷新/检查接口可能需要 buvid3 过风控
        except Exception:  # noqa: BLE001
            pass
        cred = Credential(
            sessdata=self.cookies.get("SESSDATA", ""),
            bili_jct=self.cookies.get("bili_jct", ""),
            buvid3=self.cookies.get("buvid3", ""),
            ac_time_value=ac_time_value,
        )
        try:
            need = await cred.check_refresh()
        except Exception as e:  # noqa: BLE001
            # 仅 -101(账号未登录) 视为登录态失效；网络等暂态错误不误判，下次再试
            if getattr(e, "code", None) == -101:
                return {"status": "expired"}
            logger.warning("[BiliCard] 检查续期出错（暂态，下次再试）: %s", e)
            return {"status": "error"}
        if not need:
            return {"status": "noop"}
        try:
            await cred.refresh()
        except Exception as e:  # noqa: BLE001
            if getattr(e, "code", None) == -101:
                return {"status": "expired"}
            logger.warning("[BiliCard] 续期出错（暂态，下次再试）: %s", e)
            return {"status": "error"}
        return {
            "status": "refreshed",
            "cookies": {
                "SESSDATA": getattr(cred, "sessdata", "") or "",
                "bili_jct": getattr(cred, "bili_jct", "") or "",
                "ac_time_value": getattr(cred, "ac_time_value", "") or "",
            },
        }

    async def get_up_info(self, mid) -> Optional[dict]:
        """获取 UP主基本信息（昵称、头像）。经 bilibili-api 库规避风控。"""
        cred = self._credential()
        if not cred:
            return None
        try:
            from bilibili_api import user

            info = await user.User(int(mid), credential=cred).get_user_info()
            return {
                "mid": info.get("mid", mid),
                "name": info.get("name", ""),
                "face": info.get("face", ""),
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("UP主信息获取失败(mid=%s): %s", mid, e)
            return None

    async def get_latest_videos(self, mid, count: int = 5) -> List[dict]:
        """获取 UP主最新投稿列表。经 bilibili-api 库规避风控，对偶发 412 重试。

        返回 [{bvid, title, created}]，created 为发布时间戳。
        """
        cred = self._credential()
        if not cred:
            return []
        from bilibili_api import user

        u = user.User(int(mid), credential=cred)
        for attempt in range(3):
            try:
                res = await u.get_videos()
                vlist = ((res or {}).get("list") or {}).get("vlist") or []
                return [
                    {
                        "bvid": v.get("bvid", ""),
                        "title": v.get("title", ""),
                        "created": v.get("created", 0),
                    }
                    for v in vlist[:count]
                ]
            except Exception as e:  # noqa: BLE001
                if attempt < 2:
                    await asyncio.sleep(2 + attempt * 2)
                    continue
                logger.warning("UP主投稿获取失败(mid=%s): %s", mid, e)
        return []

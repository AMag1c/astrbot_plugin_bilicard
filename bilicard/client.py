"""B站数据抓取层：视频信息、在线人数、热门评论、字幕、UP主投稿等接口封装。

所有方法在失败时返回 None / 空列表，并记录日志，不向上抛异常。
"""

import asyncio
import base64
import logging
import re
from typing import List, Optional, Tuple

import aiohttp

from . import wbi

logger = logging.getLogger(__name__)

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


class BiliClient:
    def __init__(self, cookies: Optional[dict] = None, timeout: int = 15):
        # cookies: {"SESSDATA": "...", "bili_jct": "..."}
        self.cookies = {k: v for k, v in (cookies or {}).items() if v}
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._buvid_ready = False

    def _headers(self, referer: str = "https://www.bilibili.com") -> dict:
        h = {"User-Agent": _UA, "Referer": referer}
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
                async with session.get(url, params=params, headers=self._headers(referer)) as resp:
                    if resp.status != 200:
                        logger.warning("请求 %s 返回 HTTP %s", url, resp.status)
                        return None
                    return await resp.json()
        except Exception as e:  # noqa: BLE001
            logger.error("请求 %s 失败: %s", url, e)
            return None

    async def _ensure_buvid(self) -> None:
        """获取 buvid3 写入 Cookie。B站 space/wbi 接口风控需要此设备标识，
        否则即使有 SESSDATA 也会返回 -352 风控校验失败。"""
        if self._buvid_ready or self.cookies.get("buvid3"):
            self._buvid_ready = True
            return
        data = await self._get_json("https://api.bilibili.com/x/frontend/finger/spi")
        b3 = ((data or {}).get("data") or {}).get("b_3", "")
        if b3:
            self.cookies["buvid3"] = b3
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

        data = await self._get_json(API_VIEW, params)
        if not data or data.get("code") != 0:
            logger.warning("视频信息获取失败: %s", (data or {}).get("message"))
            return None

        d = data["data"]
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
            "pubdate": d.get("pubdate", 0),     # unix 时间戳
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
            result.append({
                "name": member.get("uname", ""),
                "avatar": member.get("avatar", ""),
                "message": (content.get("message", "") or "").strip(),
                "like": r.get("like", 0),
            })
        return result

    async def get_subtitle_text(
        self, bvid: str, cid: int, max_len: int = 4000
    ) -> Optional[str]:
        """获取视频字幕纯文本。先用自有 wbi 实现，拿不到再回退 bilibili-api 库。"""
        if not self.cookies.get("SESSDATA"):
            logger.info("未配置 SESSDATA，跳过字幕获取")
            return None
        subtitles = await self._subtitle_list_wbi(bvid, cid)
        if not subtitles:
            logger.info("wbi 未取到字幕，回退 bilibili-api 库")
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
            return ((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
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

"""抖音分享解析：口令 / 短链 / 网页地址 → 作品信息与无水印视频直链。

走公开分享页 ``https://www.iesdouyin.com/share/video/{aweme_id}``，解析页面内嵌的
``window._ROUTER_DATA`` JSON。这条路**不需要 a_bogus 签名、不需要登录**，依赖面最小；
代价是抖音风控收紧时可能只返回页面骨架。三个已知的成败关键：

1. 移动端 UA + ``Accept: text/html…``，否则抖音不做服务端渲染；
2. **不带** douyin.com 的 Referer，跨站来源会让 item_list 变空；
3. 先注册游客 ``ttwid`` 并随请求带上，否则同样拿不到作品数据。

取数据分三级：分享页 → iteminfo 接口 → 页面正则兜底，任一成功即可出卡片。

失败一律返回 None 并记日志，不向上抛异常（与 client.py 一致）。

不依赖 AstrBot，可脱框架单测。
"""

import asyncio
import base64
import json
import re
from typing import Any, Optional

import aiohttp

from .log import logger

# 分享页只对移动端 UA 做服务端渲染，桌面 UA 拿不到 _ROUTER_DATA
_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

# 末尾不要加斜杠：带斜杠会多一次跳转，且实测拿不到渲染好的数据
SHARE_PAGE = "https://www.iesdouyin.com/share/video/{}"
WEB_PAGE = "https://www.douyin.com/video/{}"
# 备用数据源：老版 iteminfo 接口，同样不需要签名，返回结构与分享页里的作品一致
API_ITEMINFO = "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/"

# 游客设备标识注册接口。抖音对没有 ttwid 的请求常常只渲染页面骨架、
# item_list 给空数组，带上它才会返回真实作品数据。无需登录。
TTWID_REGISTER = "https://ttwid.bytedance.com/ttwid/union/register/"
_TTWID_PAYLOAD = (
    '{"region":"cn","aid":1768,"needFid":false,'
    '"service":"www.ixigua.com","migrate_info":{"ticket":"","source":"node"},'
    '"cbUrlProtocol":"https","union":true}'
)

# 口令里的短链，形如 https://v.douyin.com/iRxxxxx/
_SHORT_RE = re.compile(r"(?:https?://)?v\.douyin\.com/[A-Za-z0-9_\-]+/?", re.IGNORECASE)
# 直接带 aweme_id 的各种地址：网页版 / 分享页 / 发现页弹窗
_ID_RE = re.compile(
    r"(?:douyin\.com/(?:video|note)/|iesdouyin\.com/share/(?:video|note|slides)/"
    r"|modal_id=)(\d{10,25})",
    re.IGNORECASE,
)
# 页面里内嵌的路由数据；只定位赋值号，JSON 边界交给括号配平
_ROUTER_ANCHOR_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*")

# 兜底用：JSON 结构变化时直接从页面文本里捞地址
_PLAY_URL_RE = re.compile(r'https?://[^"\\\s\']*?/aweme/v1/play[^"\\\s\']*')
_VOD_URL_RE = re.compile(r'https?://[^"\\\s\']*?\.douyinvod\.com/[^"\\\s\']*')
_COVER_URL_RE = re.compile(
    r'https?://[^"\\\s\']*?\.(?:douyinpic|byteimg)\.com/[^"\\\s\']*'
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)

# 画质名 → 短边像素上限（抖音多为竖屏，清晰度看短边）
QUALITY_SHORT_SIDE = {"1080P": 1080, "720P": 720, "540P": 540}
# 画质名 → 地址里的 ratio 值。只留三档：实测 480p/360p 抖音不认，且不是退回默认
# 而是返回更大的文件（同片 720p=69MB、540p=60MB，480p/360p 都是 92MB）
QUALITY_RATIO = {"1080P": "1080p", "720P": "720p", "540P": "540p"}
_RATIO_RE = re.compile(r"([?&])ratio=[^&]*")


def find_link(text: str) -> Optional[str]:
    """从文本中提取抖音作品地址，找不到返回 None。

    抖音分享到 QQ 多是一段"口令"，形如::

        7.86 gJb:/ 复制打开抖音，看看【某某的作品】... https://v.douyin.com/iRxxxxx/

    前面那串识别码不用管，取出其中的短链即可；也兼容直接粘的网页地址与小程序
    卡片 JSON 里的链接。
    """
    if not text:
        return None
    m = _SHORT_RE.search(text)
    if m:
        url = m.group(0)
        return url if url.startswith("http") else "https://" + url
    m = _ID_RE.search(text)
    if m:
        return SHARE_PAGE.format(m.group(1))
    return None


def _extract_json_object(text: str, start: int) -> str:
    """从 ``text[start]`` 处的 ``{`` 起按括号配平截出完整 JSON 文本。

    必须跳过字符串字面量内的花括号与转义——抖音文案里常出现 ``{`` ``}``，
    单纯数括号会提前收尾，截出半截 JSON。
    """
    if start < 0 or start >= len(text) or text[start] != "{":
        return ""
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if in_str and c == "\\":
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return ""


def strip_watermark(url: str) -> str:
    """把带水印的播放地址换成无水印地址。

    抖音的 ``play_addr`` 常是 ``.../aweme/v1/playwm/?video_id=xxx``，把 ``playwm``
    换成 ``play`` 即为无水印版本；已是无水印的不受影响。
    """
    if not url:
        return url
    return url.replace("playwm", "play")


class DouyinClient:
    """抖音作品信息抓取（无需登录）。"""

    def __init__(self, timeout: int = 15):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._ttwid = ""  # 游客设备标识，进程内缓存复用
        self._ttwid_lock = asyncio.Lock()

    async def _get_ttwid(self, refresh: bool = False) -> str:
        """取游客 ttwid（带缓存）。拿不到返回空串，不影响后续请求。"""
        async with self._ttwid_lock:
            if self._ttwid and not refresh:
                return self._ttwid
            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.post(
                        TTWID_REGISTER,
                        data=_TTWID_PAYLOAD,
                        headers={
                            "User-Agent": _UA,
                            "Content-Type": "application/json; charset=utf-8",
                        },
                    ) as resp:
                        cookie = resp.cookies.get("ttwid")
                        if cookie is not None:
                            self._ttwid = cookie.value
            except Exception as e:  # noqa: BLE001
                logger.debug("[BiliCard] 获取 ttwid 失败（将无 Cookie 请求）: %s", e)
            return self._ttwid

    def _headers(self) -> dict:
        """请求分享页 / 跟随短链用。

        ⚠ 两个细节决定成败：必须带 ``Accept: text/html…``（否则抖音不做服务端
        渲染，页面里就没有数据），且**不能带 douyin.com 的 Referer**（跨站来源会
        让 iesdouyin 返回空的 item_list）。
        """
        h = {
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if self._ttwid:
            h["Cookie"] = f"ttwid={self._ttwid}"
        return h

    def _api_headers(self) -> dict:
        """请求 iteminfo 接口用（JSON）。"""
        return {
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.douyin.com/",
        }

    def _media_headers(self) -> dict:
        """下载封面图用。"""
        return {"User-Agent": _UA, "Referer": "https://www.douyin.com/"}

    async def resolve_aweme_id(self, url: str) -> Optional[str]:
        """把任意抖音地址归一化成 aweme_id（短链会跟随重定向）。"""
        m = _ID_RE.search(url or "")
        if m:
            return m.group(1)
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(
                    url, headers=self._headers(), allow_redirects=True
                ) as resp:
                    final = str(resp.url)
        except Exception as e:  # noqa: BLE001
            logger.warning("[BiliCard] 抖音短链解析失败 %s: %s", url, e)
            return None
        m = _ID_RE.search(final)
        if not m:
            logger.warning("[BiliCard] 抖音短链跳转后仍无 aweme_id：%s", final)
            return None
        return m.group(1)

    async def get_video_info(self, aweme_id: str) -> Optional[dict]:
        """抓分享页并解析出标准化的作品信息。

        Returns:
            ``{aweme_id, title, cover, duration, like, video_url, streams,
            share_url}``；解析不出则 None。``video_url`` 为无水印直链，图集作品
            没有它。
        """
        html = ""
        # 两轮：先用缓存的 ttwid，空手而归就刷新一个新的再试（抖音常对陈旧/缺失
        # 的游客标识只返回页面骨架）
        for attempt in (0, 1):
            await self._get_ttwid(refresh=attempt == 1)
            html = await self._fetch(SHARE_PAGE.format(aweme_id))
            if not html:
                continue
            data = self._extract_router_data(html)
            if data is None:
                logger.warning(
                    "[BiliCard] 抖音分享页未找到 _ROUTER_DATA（页面结构可能已变）"
                )
                break
            item = self._find_item(data)
            if item:
                return self._normalize(item, aweme_id)
            # 没找到作品：先看是不是被平台限制，那不是插件的问题
            reason = self._find_filter_reason(data)
            if reason:
                logger.warning("[BiliCard] 抖音拒绝提供该作品：%s", reason)
                return None
            if attempt == 0:
                continue  # 换个 ttwid 再试一次
            logger.warning(
                "[BiliCard] 抖音分享页没返回作品数据 aweme_id=%s；%s"
                "（item_list=0 且 filter_list=0 多为服务器 IP 被风控）",
                aweme_id,
                self._describe_video_info_res(data),
            )

        # 备用数据源：老版 iteminfo 接口
        item = await self._fetch_by_api(aweme_id)
        if item:
            logger.debug("[BiliCard] 分享页解析失败，已改用 iteminfo 接口取到作品")
            return self._normalize(item, aweme_id)

        # 最后兜底：绕开 JSON 结构，直接从页面文本里捞播放地址
        salvaged = self._salvage(html, aweme_id)
        if salvaged:
            logger.debug("[BiliCard] 已用兜底方式从页面提取到抖音视频地址")
            return salvaged
        return None

    async def _fetch_by_api(self, aweme_id: str) -> Optional[dict]:
        """备用数据源：老版 iteminfo 接口（无需签名）。取不到返回 None。"""
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(
                    API_ITEMINFO,
                    params={"item_ids": aweme_id},
                    headers=self._api_headers(),
                ) as resp:
                    if resp.status != 200:
                        logger.debug("[BiliCard] iteminfo 返回 HTTP %s", resp.status)
                        return None
                    data = await resp.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            logger.debug("[BiliCard] iteminfo 请求失败: %s", e)
            return None
        items = (data or {}).get("item_list") or []
        if items and isinstance(items[0], dict) and items[0]:
            return items[0]
        return None

    @classmethod
    def _salvage(cls, html: str, aweme_id: str) -> Optional[dict]:
        """结构解析失败时的兜底：用正则直接从页面里找视频与封面地址。

        页面里的 JSON 是转义过的（``\\u002F`` / ``\\/``），先还原再匹配。
        拿到的信息不全（没有点赞数等），但至少能把视频和链接送出去。
        """
        text = html.replace("\\u002F", "/").replace("\\/", "/")
        play = ""
        for rx in (_PLAY_URL_RE, _VOD_URL_RE):
            m = rx.search(text)
            if m:
                play = m.group(0)
                break
        if not play:
            return None
        cover = ""
        m = _COVER_URL_RE.search(text)
        if m:
            cover = m.group(0)
        title = ""
        m = _TITLE_RE.search(html)
        if m:
            title = m.group(1).strip()
        return {
            "aweme_id": aweme_id,
            "title": title,
            "cover": cover,
            "duration": 0,
            "like": 0,
            "video_url": strip_watermark(play),
            "streams": [],
            "share_url": WEB_PAGE.format(aweme_id),
        }

    async def fetch_image_data_uri(self, url: str) -> str:
        """下载封面并转 data URI，规避防盗链。失败则退回原 URL。

        用抖音自己的请求头，**不带任何 B站 Cookie**。
        """
        if not url:
            return url
        if url.startswith("//"):
            url = "https:" + url
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, headers=self._media_headers()) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "[BiliCard] 抖音封面下载返回 HTTP %s", resp.status
                        )
                        return url
                    ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                    raw = await resp.read()
            return f"data:{ctype};base64,{base64.b64encode(raw).decode()}"
        except Exception as e:  # noqa: BLE001
            logger.warning("[BiliCard] 抖音封面下载失败 %s: %s", url, e)
            return url

    async def _fetch(self, url: str) -> str:
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, headers=self._headers()) as resp:
                    if resp.status != 200:
                        logger.warning("[BiliCard] 抖音分享页返回 HTTP %s", resp.status)
                        return ""
                    return await resp.text()
        except Exception as e:  # noqa: BLE001
            logger.warning("[BiliCard] 抖音分享页请求失败 %s: %s", url, e)
            return ""

    @staticmethod
    def _extract_router_data(html: str) -> Optional[dict]:
        """取出页面里的 window._ROUTER_DATA。

        不靠 ``</script>`` 收尾（后面往往还有别的语句），而是从赋值号后的第一个
        ``{`` 起做括号配平，直接定位 JSON 边界。
        """
        m = _ROUTER_ANCHOR_RE.search(html)
        if not m:
            return None
        start = html.find("{", m.end())
        if start < 0:
            return None
        raw = _extract_json_object(html, start)
        if not raw:
            logger.warning("[BiliCard] 抖音 _ROUTER_DATA 括号不配平，截取失败")
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("[BiliCard] 抖音 _ROUTER_DATA 非法 JSON: %s", e)
            return None

    @staticmethod
    def _looks_like_item(node: Any) -> bool:
        """判断一个节点是不是作品条目。

        抖音页面的 key（``video_(id)/page`` / ``videoInfoRes`` / ``item_list``…）
        版本间会变，写死路径很容易失效，故改为按**特征**判定：带 aweme_id，且有
        video / images / desc 之一。
        """
        if not isinstance(node, dict) or not node.get("aweme_id"):
            return False
        return (
            isinstance(node.get("video"), dict)
            or isinstance(node.get("images"), list)
            or "desc" in node
        )

    @classmethod
    def _walk(cls, data: Any, limit: int = 50000):
        """深度遍历 JSON 里的所有 dict 节点（抖音数据很大，上限放宽）。"""
        stack = [data]
        seen = 0
        while stack and seen < limit:
            node = stack.pop()
            seen += 1
            if isinstance(node, dict):
                yield node
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    @classmethod
    def _find_item(cls, data: dict) -> Optional[dict]:
        """在路由数据里找到作品条目（按特征匹配，不依赖固定路径）。"""
        for node in cls._walk(data):
            if cls._looks_like_item(node):
                return node
        return None

    @classmethod
    def _describe_video_info_res(cls, data: dict) -> str:
        """概括 videoInfoRes 的关键计数，用于日志定位。

        item_list=0 且 filter_list=0 基本可断定是"没拿到数据"（风控），而不是
        解析路径写错了。
        """
        for node in cls._walk(data):
            vir = node.get("videoInfoRes")
            if isinstance(vir, dict):
                return (
                    f"item_list={len(vir.get('item_list') or [])} "
                    f"filter_list={len(vir.get('filter_list') or [])} "
                    f"status_code={vir.get('status_code')}"
                )
        keys = list(data.keys())[:6]
        loader = data.get("loaderData")
        sub = list(loader.keys())[:6] if isinstance(loader, dict) else "无"
        return f"未见 videoInfoRes；顶层={keys}，loaderData={sub}"

    @classmethod
    def _find_filter_reason(cls, data: dict) -> str:
        """取出抖音给的受限说明。

        作品需要验证、已删除或有地区限制时，``item_list`` 会是空的，真正的原因
        放在 ``filter_list`` 里。把它捞出来，用户才知道是被平台拒绝而非插件坏了。
        """
        for node in cls._walk(data):
            filt = node.get("filter_list")
            if isinstance(filt, list) and filt and isinstance(filt[0], dict):
                f = filt[0]
                for key in ("detail_msg", "filter_reason", "notice", "msg"):
                    msg = f.get(key)
                    if isinstance(msg, str) and msg.strip():
                        return msg.strip()
        return ""

    @staticmethod
    def _first_url(node: Any) -> str:
        """从 {"url_list": [...]} 这类结构里取第一个可用地址。"""
        if isinstance(node, dict):
            for key in ("url_list", "urlList"):
                urls = node.get(key)
                if isinstance(urls, list):
                    for u in urls:
                        if isinstance(u, str) and u.startswith("http"):
                            return u
            for key in ("url", "uri"):
                u = node.get(key)
                if isinstance(u, str) and u.startswith("http"):
                    return u
        elif isinstance(node, str) and node.startswith("http"):
            return node
        return ""

    @classmethod
    def _normalize(cls, item: dict, aweme_id: str) -> dict:
        video = item.get("video") or {}
        stat = item.get("statistics") or {}

        play = cls._first_url(video.get("play_addr") or video.get("playAddr"))
        if not play:
            play = cls._first_url(video.get("download_addr"))
        cover = cls._first_url(
            video.get("cover")
            or video.get("origin_cover")
            or video.get("dynamic_cover")
        )

        duration_ms = video.get("duration") or item.get("duration") or 0
        try:
            duration = int(duration_ms) // 1000
        except (TypeError, ValueError):
            duration = 0

        return {
            "aweme_id": item.get("aweme_id") or aweme_id,
            "title": (item.get("desc") or "").strip(),
            "cover": cover,
            "duration": duration,
            "like": stat.get("digg_count") or 0,
            "video_url": strip_watermark(play),
            "streams": cls._collect_streams(video),
            "share_url": WEB_PAGE.format(item.get("aweme_id") or aweme_id),
        }

    @classmethod
    def _collect_streams(cls, video: dict) -> list:
        """收集可选画质档位（``video.bit_rate`` 数组），按清晰度从高到低排序。

        每档自带 ``data_size``（精确字节数），因此选档时不用像 B站那样按码率
        估算体积。没有该数组时返回空列表，调用方退回默认 play_addr。
        """
        raw = video.get("bit_rate")
        if not isinstance(raw, list):
            return []  # 分享页给的是空值/非数组，遍历会直接抛 TypeError
        streams = []
        for br in raw:
            if not isinstance(br, dict):
                continue
            addr = br.get("play_addr") or br.get("playAddr") or {}
            url = cls._first_url(addr)
            if not url:
                continue
            try:
                width = int(addr.get("width", 0) or 0)
                height = int(addr.get("height", 0) or 0)
                size = int(addr.get("data_size", 0) or 0)
                bitrate = int(br.get("bit_rate", 0) or 0)
            except (TypeError, ValueError):
                continue
            streams.append(
                {
                    "url": strip_watermark(url),
                    "width": width,
                    "height": height,
                    "size": size,
                    "bitrate": bitrate,
                    "gear": str(br.get("gear_name", "")),
                }
            )
        # 竖屏视频的"清晰度"看短边，故用 min(w, h) 排序；同分辨率再比码率
        streams.sort(
            key=lambda s: (min(s["width"], s["height"]), s["bitrate"]), reverse=True
        )
        return streams


def describe_stream(s: dict) -> str:
    """把一个画质档位说成人话，用于日志（体积另有下载结果记录，这里不重复）。"""
    if not s:
        return "默认"
    px = min(s.get("width", 0), s.get("height", 0))
    name = f"{px}P" if px else "未知分辨率"
    gear = s.get("gear")
    return f"{name}（{gear}）" if gear else name


def apply_ratio(url: str, quality: str) -> str:
    """改写播放地址里的 ``ratio`` 参数来换清晰度。

    分享页只给一个 play_addr（``bit_rate`` 是空的，完整多档要 Web API + a_bogus
    签名），改这个参数是唯一免签名的画质控制手段。地址里没有它时原样返回。
    """
    target = QUALITY_RATIO.get(quality)
    if not target or not url or "ratio=" not in url:
        return url
    return _RATIO_RE.sub(rf"\1ratio={target}", url)


def pick_video_url(info: dict, quality: str = "720P", max_size_mb: int = 0) -> tuple:
    """按画质与体积上限挑一个视频流，返回 ``(url, 档位描述)``。

    有多档信息时按精确体积筛选（不必下到一半才中止），没有则改写 ratio 参数。
    取不到合适档位时返回 ``("", "")``，由调用方给出提示。

    Args:
        info: :meth:`DouyinClient.get_video_info` 的结果。
        quality: 见 :data:`QUALITY_RATIO`。
        max_size_mb: 体积上限，0 表示不限制。
    """
    streams = info.get("streams") or []
    max_bytes = max(int(max_size_mb), 0) * 1024 * 1024
    if not streams:
        # 分享页没给多档，退而改写地址里的 ratio 参数；体积交给下载时校验。
        # 档位不受支持、或地址里压根没这个参数时，如实报"默认"，别谎报画质
        raw = info.get("video_url", "")
        applied = bool(QUALITY_RATIO.get(quality)) and "ratio=" in raw
        return apply_ratio(raw, quality), quality if applied else "默认"

    limit_px = QUALITY_SHORT_SIDE.get(quality, 0)
    for s in streams:  # 已按清晰度降序
        short = min(s["width"], s["height"])
        if limit_px and short and short > limit_px:
            continue
        if max_bytes and s["size"] and s["size"] > max_bytes:
            continue
        return s["url"], describe_stream(s)

    if max_bytes:
        smallest = streams[-1]
        if smallest["size"] and smallest["size"] > max_bytes:
            return "", ""  # 最低档也超限，别白下
    return streams[-1]["url"], describe_stream(streams[-1])

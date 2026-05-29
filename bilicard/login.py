"""B站扫码登录：申请二维码 -> 轮询扫码状态 -> 从成功回调的 url 解析出 Cookie。"""

import logging
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

import aiohttp

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
QRCODE_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QRCODE_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# poll 返回的状态码含义
CODE_SUCCESS = 0       # 登录成功
CODE_EXPIRED = 86038   # 二维码已失效


def _headers() -> dict:
    return {"User-Agent": _UA, "Referer": "https://www.bilibili.com"}


async def generate_qrcode() -> Optional[Tuple[str, str]]:
    """申请登录二维码，返回 (qrcode_key, 二维码内容url)。"""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(QRCODE_GEN, headers=_headers()) as r:
                d = await r.json()
        if d.get("code") != 0:
            logger.warning("申请二维码失败: %s", d.get("message"))
            return None
        data = d["data"]
        return data["qrcode_key"], data["url"]
    except Exception as e:  # noqa: BLE001
        logger.error("申请二维码异常: %s", e)
        return None


async def poll_qrcode(qrcode_key: str) -> Tuple[int, Optional[dict]]:
    """轮询扫码状态。

    返回 (code, cookies)。code==0 时 cookies 为 {SESSDATA, bili_jct, DedeUserID}。
    """
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                QRCODE_POLL, params={"qrcode_key": qrcode_key}, headers=_headers()
            ) as r:
                d = await r.json()
        data = d.get("data") or {}
        code = data.get("code", -1)
        if code == CODE_SUCCESS:
            q = parse_qs(urlparse(data.get("url", "")).query)
            cookies = {
                "SESSDATA": q.get("SESSDATA", [""])[0],
                "bili_jct": q.get("bili_jct", [""])[0],
                "DedeUserID": q.get("DedeUserID", [""])[0],
            }
            return CODE_SUCCESS, cookies
        return code, None
    except Exception as e:  # noqa: BLE001
        logger.error("轮询扫码状态异常: %s", e)
        return -1, None


def make_qr_image(content: str, path: str) -> bool:
    """把二维码内容渲染成 PNG 图片，成功返回 True。需要 qrcode[pil]。"""
    try:
        import qrcode

        img = qrcode.make(content)
        img.save(path)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("生成二维码图片失败（请确认已安装 qrcode[pil]）: %s", e)
        return False

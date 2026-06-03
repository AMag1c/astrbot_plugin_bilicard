"""B站 WBI 签名工具。

部分 B站接口（如 player/wbi/v2 字幕接口）要求对查询参数做 WBI 签名，
否则返回数据为空或被风控。本模块负责获取 mixin_key 并生成 w_rid 签名。

参考实现思路与 nonebot / 各开源插件一致。
"""

import hashlib
import time
import urllib.parse
from typing import Optional, Tuple

import aiohttp

from .log import logger

# WBI 混淆表（B站固定）。fmt: off 保留 16×4 网格，避免被展开成一项一行。
# fmt: off
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
# fmt: on

# mixin_key 缓存：(mixin_key, 获取时间)
_wbi_cache: Optional[Tuple[str, float]] = None
_WBI_CACHE_TTL = 86400  # 24 小时

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _get_mixin_key(img_key: str, sub_key: str) -> str:
    """由 img_key + sub_key 生成 32 位 mixin_key。"""
    orig = img_key + sub_key
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


async def _fetch_mixin_key(cookies: Optional[dict] = None) -> Optional[str]:
    """从 nav 接口获取 img_key / sub_key 并生成 mixin_key（带 24h 缓存）。"""
    global _wbi_cache

    if _wbi_cache:
        mixin_key, fetch_time = _wbi_cache
        if time.time() - fetch_time < _WBI_CACHE_TTL:
            return mixin_key

    headers = {"User-Agent": _UA, "Referer": "https://www.bilibili.com"}
    if cookies:
        parts = [f"{k}={v}" for k, v in cookies.items() if v]
        if parts:
            headers["Cookie"] = "; ".join(parts)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(NAV_URL, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("获取 WBI keys 失败, HTTP %s", resp.status)
                    return None
                data = await resp.json()

        wbi_img = (data.get("data") or {}).get("wbi_img") or {}
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")
        if not img_url or not sub_url:
            logger.warning("WBI img_url / sub_url 为空")
            return None

        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        mixin_key = _get_mixin_key(img_key, sub_key)
        _wbi_cache = (mixin_key, time.time())
        logger.debug("WBI mixin_key 已更新")
        return mixin_key
    except Exception as e:  # noqa: BLE001
        logger.error("获取 WBI keys 异常: %s", e)
        return None


async def sign_params(params: dict, cookies: Optional[dict] = None) -> dict:
    """对参数做 WBI 签名，返回追加了 wts 与 w_rid 的新字典。

    若无法获取 mixin_key，则原样返回（调用方应能容忍未签名的降级情况）。
    """
    mixin_key = await _fetch_mixin_key(cookies)
    if not mixin_key:
        logger.warning("无法获取 WBI mixin_key，跳过签名")
        return dict(params)

    signed = dict(params)
    signed["wts"] = int(time.time())

    # 按 key 字典序排序后拼 query
    sorted_params = dict(sorted(signed.items()))
    query = urllib.parse.urlencode(sorted_params)
    # 过滤 B站要求剔除的特殊字符
    for ch in "!'()*":
        query = query.replace(urllib.parse.quote(ch), "")

    signed["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return signed

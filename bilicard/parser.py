"""从文本中提取 B站视频标识。

仅做纯正则匹配，不发起网络请求；b23 短链的重定向解析由 client 负责。
"""

import re
from typing import Optional, Tuple

# BV 号：BV 开头 + 10 位字母数字
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
# av 号：av + 数字
AV_RE = re.compile(r"av(\d{1,12})", re.IGNORECASE)
# b23.tv / bili2233.cn 短链
B23_RE = re.compile(r"(?:https?://)?(?:b23\.tv|bili2233\.cn)/[0-9A-Za-z]+")
# 标准视频页链接
BILI_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)",
    re.IGNORECASE,
)


def find_video_token(text: str) -> Optional[Tuple[str, str]]:
    """从文本中提取第一个 B站视频标识。

    返回 (kind, value)：
    - ('bv', 'BVxxxxxxxxxx')
    - ('av', '12345')           # 不含 av 前缀的纯数字
    - ('b23', 'https://b23.tv/xxxx')  # 短链，需后续重定向解析

    找不到则返回 None。匹配优先级：短链 > 标准链接 > 裸 BV 号 > 裸 av 号。
    """
    if not text:
        return None

    # 短链最优先：小程序 / 卡片分享通常是短链
    m = B23_RE.search(text)
    if m:
        url = m.group(0)
        if not url.startswith("http"):
            url = "https://" + url
        return ("b23", url)

    # 标准视频页链接
    m = BILI_URL_RE.search(text)
    if m:
        token = m.group(1)
        if token.lower().startswith("av"):
            return ("av", token[2:])
        return ("bv", token)

    # 裸 BV 号
    m = BV_RE.search(text)
    if m:
        return ("bv", m.group(1))

    # 裸 av 号
    m = AV_RE.search(text)
    if m:
        return ("av", m.group(1))

    return None

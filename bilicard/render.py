"""渲染数据组装层。

负责把 client 抓取到的原始数据格式化成模板友好的结构，并加载 HTML 模板、
B站官方图标字体（vanfont）。图片需在外部下载好转成 data URI 后塞进 info。
"""

import base64
import os
import time
from typing import List, Optional

_TMPL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "templates", "card.html"
)
_VANFONT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "assets", "vanfont.woff"
)
_vanfont_cache: Optional[str] = None

# 统计项 -> vanfont 码点（B站官方图标字体 dashuchufang/bili_icon_pack）
_STAT_ICON = {
    "view": chr(0xE6E6),      # info_playnumber 播放
    "danmaku": chr(0xE6E7),   # info_barragenumber 弹幕
    "like": chr(0xE6E0),      # videodetails_like 点赞
    "coin": chr(0xE6E4),      # videodetails_throw 投币
    "favorite": chr(0xE6E1),  # videodetails_collec 收藏
    "share": chr(0xE70F),     # videodetails_share 分享
    "reply": chr(0xE639),     # pinglun 评论
}
_LIKE_ICON = chr(0xE6E0)   # 评论点赞小图标
_LOGO_ICON = chr(0xE725)   # Navbar_logo —— B站 logo 字形（非斜体）

# 单条评论最大显示字数：约 1.3 行（第一行满 + 第二行约 1/3 处即截断），更克制好看
_COMMENT_MAX_LEN = 38


def load_template() -> str:
    """读取 HTML 模板字符串。"""
    with open(_TMPL_PATH, encoding="utf-8") as f:
        return f.read()


def load_vanfont_b64() -> str:
    """读取 B站 vanfont 图标字体并转 base64（带缓存），用于模板内嵌 @font-face。"""
    global _vanfont_cache
    if _vanfont_cache is None:
        try:
            with open(_VANFONT_PATH, "rb") as f:
                _vanfont_cache = base64.b64encode(f.read()).decode()
        except Exception:
            _vanfont_cache = ""
    return _vanfont_cache


def format_count(n) -> str:
    """B站风格的数字缩写：<1万原样，1万~1亿用“万”，>=1亿用“亿”。"""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "0"
    if n < 10000:
        return str(n)
    if n < 100000000:
        return f"{n / 10000:.1f}".rstrip("0").rstrip(".") + "万"
    return f"{n / 100000000:.1f}".rstrip("0").rstrip(".") + "亿"


def format_duration(sec) -> str:
    """秒 -> mm:ss 或 hh:mm:ss。"""
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        sec = 0
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_date(ts) -> str:
    """unix 时间戳 -> YYYY-MM-DD。"""
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def build_template_data(
    info: dict,
    online_text: Optional[str] = None,
    comments: Optional[List[dict]] = None,
    summary: Optional[str] = None,
    show_bvid: bool = True,
    show_post_bar: bool = True,
) -> dict:
    """组装 Jinja2 模板渲染数据。

    info 中的 cover、owner.face 若已是 data URI 会被直接使用。
    """
    stat = info.get("stat", {})
    owner = info.get("owner", {})

    # 顺序严格对应效果图：播放/弹幕/点赞/投币/收藏/分享/评论
    stat_defs = [
        ("view", "播放"), ("danmaku", "弹幕"), ("like", "点赞"),
        ("coin", "投币"), ("favorite", "收藏"), ("share", "分享"), ("reply", "评论"),
    ]
    stats = [
        {
            "key": k,
            "label": lbl,
            "value": format_count(stat.get(k)),
            "icon": _STAT_ICON.get(k, ""),
        }
        for k, lbl in stat_defs
    ]

    comments_fmt = []
    for c in (comments or []):
        msg = (c.get("message") or "").strip()
        if not msg:
            continue
        if len(msg) > _COMMENT_MAX_LEN:
            msg = msg[:_COMMENT_MAX_LEN].rstrip() + "…"
        comments_fmt.append({
            "name": c.get("name", ""),
            "message": msg,
            "like": format_count(c.get("like")),
        })

    return {
        "show_post_bar": show_post_bar,
        "vanfont_b64": load_vanfont_b64(),
        "logo_icon": _LOGO_ICON,
        "like_icon": _LIKE_ICON,
        "up_name": owner.get("name", ""),
        "up_face": owner.get("face", ""),
        "online_text": online_text,
        "cover": info.get("cover", ""),
        "duration_text": format_duration(info.get("duration")),
        "title": info.get("title", ""),
        "pubdate_text": format_date(info.get("pubdate")),
        "bvid": info.get("bvid", "") if show_bvid else "",
        "desc": (info.get("desc") or "").strip(),
        "stats": stats,
        "comments": comments_fmt,
        "summary": summary,
    }

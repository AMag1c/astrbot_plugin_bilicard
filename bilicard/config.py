"""配置访问统一入口。

把"读哪个键、默认值是多少"集中到一处，避免默认值散落 main 多处导致与
``_conf_schema.json`` 漂移。``DEFAULTS`` 是顶层标量配置的唯一默认值来源。

不依赖 AstrBot，可用普通 dict 构造，便于脱框架单测。
"""

from typing import Any, Optional

# 顶层标量默认值（与 _conf_schema.json 对齐；list/dict 型默认见各自调用处）
DEFAULTS: dict = {
    "enable_comments": True,
    "comment_count": 3,
    "enable_ai_summary": True,
    "llm_provider_id": "",
    "summary_max_subtitle": 4000,
    "summary_max_chars": 120,
    "show_link": True,
    "render_timeout": 50,  # 在线 html_render 出图超时（秒）；超时放弃不发卡片
    "access_mode": "all",
    "cooldown_seconds": 60,
    "enable_subscribe_push": True,
    "check_interval_minutes": 10,
}

# object 型配置分组的默认值（同样必须与 _conf_schema.json 对齐）。
# 两个平台各一组，互不影响：B站视频动辄上百 MB 且要选画质，抖音则短小、只有一档。
GROUP_DEFAULTS: dict = {
    "bili_video": {
        "enabled": False,  # 自动解析出卡片后，跟着下载并发送视频
        "on_subscribe_push": False,  # 订阅推送新投稿时也发视频
        "quality": "720P",
        "max_size_mb": 100,
        "timeout": 300,  # 下载 + ffmpeg 合并总超时（秒）
        "send_mode": "video",  # video=视频消息（可直接播放）/ file=群文件
    },
    "douyin_video": {
        "enabled": False,
        "quality": "720P",  # 抖音默认档；可选见 QUALITY_RATIO
        "max_size_mb": 100,
        "timeout": 180,  # 抖音视频短，超时比 B站小
        "send_mode": "video",
    },
}


class Config:
    """AstrBotConfig 的薄包装：统一默认值与访问方式。"""

    def __init__(self, raw: Any):
        self.raw = raw if raw is not None else {}

    def get(self, key: str, default: Any = None) -> Any:
        """取顶层配置；缺省时优先回退 DEFAULTS，再回退入参 default。"""
        fallback = DEFAULTS[key] if key in DEFAULTS else default
        return self.raw.get(key, fallback)

    def int(self, key: str, default: Optional[int] = None) -> int:
        """取整型配置（容错：空/非法回退默认）。"""
        base = default if default is not None else DEFAULTS.get(key, 0)
        try:
            return int(self.raw.get(key, base) or base)
        except (TypeError, ValueError):
            return int(base)

    def bool(self, key: str) -> bool:
        return bool(self.get(key))

    def group(self, name: str) -> dict:
        """取 object 型配置分组（如 bili_video / douyin_video）。

        缺失或留空的子项用 :data:`GROUP_DEFAULTS` 补齐，调用处拿到的永远是完整
        字典，不必再写字面默认值。
        """
        base = dict(GROUP_DEFAULTS.get(name, {}))
        raw = self.raw.get(name)
        if isinstance(raw, dict):
            # 空字符串视为"未填"，回退默认；False / 0 是有效值必须保留
            base.update({k: v for k, v in raw.items() if v is not None and v != ""})
        return base

    def save(self) -> None:
        save = getattr(self.raw, "save_config", None)
        if callable(save):
            save()

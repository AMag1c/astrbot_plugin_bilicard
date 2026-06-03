"""配置访问统一入口。

把"读哪个键、默认值是多少"集中到一处，避免默认值散落 main 多处导致与
``_conf_schema.json`` 漂移。``DEFAULTS`` 是顶层标量配置的唯一默认值来源。

不依赖 AstrBot，可用普通 dict 构造，便于脱框架单测。
"""

from typing import Any, Optional

# 顶层标量默认值（与 _conf_schema.json 对齐；list/dict 型默认见各自调用处）
DEFAULTS: dict = {
    "trigger_mode": "all",
    "enable_comments": True,
    "comment_count": 3,
    "enable_ai_summary": True,
    "llm_provider_id": "",
    "summary_max_subtitle": 4000,
    "summary_max_chars": 120,
    "show_link": True,
    "access_mode": "all",
    "cooldown_seconds": 60,
    "enable_subscribe_push": True,
    "check_interval_minutes": 10,
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

    def save(self) -> None:
        save = getattr(self.raw, "save_config", None)
        if callable(save):
            save()

"""插件统一日志出口。

裸 ``logging.getLogger(__name__)`` 在 AstrBot 里只会冒泡到 root → 进控制台/docker 日志，
**到不了 WebUI 日志面板**（WebUI 只采集 ``astrbot.api.logger``）。子模块统一走本适配层后，
client/wbi/login 等的诊断日志（风控、Cookie 失效、下载失败）才能在 WebUI 看到。

框架内复用 ``astrbot.api.logger``；脱离框架（``_localtest_*`` 单测）回退标准 logging。
"""

try:
    from astrbot.api import logger  # type: ignore
except Exception:  # noqa: BLE001 —— 脱框架单测：回退标准 logging
    import logging

    logger = logging.getLogger("astrbot_plugin_bilicard")
    if not logger.handlers:
        _handler = logging.StreamHandler()
        _handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(_handler)
        logger.setLevel(logging.INFO)

__all__ = ["logger"]

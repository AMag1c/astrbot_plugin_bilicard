"""B站登录凭证（SESSDATA / bili_jct / ac_time_value / login_umo）持久化。

存于 data 目录下 credential.json（原子写，避免崩溃损坏）。扫码登录得到的凭证
比手填配置优先。``ac_time_value`` 即 refresh_token（用于自动续期，非 Cookie）；
``login_umo`` 记录最近一次登录所在会话，便于续期失效时回该会话提醒管理员。
与 main 解耦，便于单测。
"""

import json
import os
import tempfile

from .log import logger


class CredentialStore:
    def __init__(self, data_dir: str):
        self._path = os.path.join(data_dir, "credential.json")

    def load(self) -> dict:
        """读取凭证 dict（含 SESSDATA/bili_jct/ac_time_value/login_umo，旧文件可能缺
        后两项）；不存在或失败返回空 dict。"""
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning("[BiliCard] 读取凭证失败: %s", e)
        return {}

    def save(
        self,
        sessdata: str = "",
        bili_jct: str = "",
        ac_time_value: str = "",
        login_umo: str = "",
    ) -> None:
        data = {
            "SESSDATA": sessdata or "",
            "bili_jct": bili_jct or "",
            "ac_time_value": ac_time_value or "",
            "login_umo": login_umo or "",
        }
        try:
            self._atomic_write(data)
        except Exception as e:  # noqa: BLE001
            logger.error("[BiliCard] 保存凭证失败: %s", e)

    def _atomic_write(self, data: dict) -> None:
        d = os.path.dirname(self._path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

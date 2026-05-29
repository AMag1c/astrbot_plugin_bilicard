"""订阅关系存储。

数据存于插件配置项 subscriptions（JSON），修改后写回配置使后台可见可编辑。
结构：{"<unified_msg_origin>": [{"mid", "name", "last_bvid"}]}
"""

import json
from typing import Callable, Dict, Iterator, List, Tuple


class SubscriptionStore:
    def __init__(self, config, save_func: Callable[[], None]):
        self.config = config
        self._save_func = save_func
        self._subs: Dict[str, List[dict]] = self._load_from_config()

    def _load_from_config(self) -> Dict[str, List[dict]]:
        raw = self.config.get("subscriptions") if self.config else None
        if not raw:
            return {}
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _persist(self) -> None:
        try:
            self.config["subscriptions"] = json.dumps(self._subs, ensure_ascii=False, indent=2)
        except Exception:
            pass
        try:
            self._save_func()
        except Exception:
            pass

    def add(self, umo: str, mid, name: str, last_bvid: str = "") -> bool:
        subs = self._subs.setdefault(umo, [])
        if any(str(s["mid"]) == str(mid) for s in subs):
            return False
        subs.append({"mid": str(mid), "name": name, "last_bvid": last_bvid})
        self._persist()
        return True

    def remove(self, umo: str, mid) -> bool:
        subs = self._subs.get(umo, [])
        new = [s for s in subs if str(s["mid"]) != str(mid)]
        if len(new) == len(subs):
            return False
        if new:
            self._subs[umo] = new
        else:
            self._subs.pop(umo, None)
        self._persist()
        return True

    def list(self, umo: str) -> List[dict]:
        return list(self._subs.get(umo, []))

    def update_last_bvid(self, umo: str, mid, bvid: str) -> None:
        for s in self._subs.get(umo, []):
            if str(s["mid"]) == str(mid):
                s["last_bvid"] = bvid
                self._persist()
                return

    def iter_items(self) -> Iterator[Tuple[str, dict]]:
        for umo, subs in self._subs.items():
            for s in list(subs):
                yield umo, s

"""健康检查结果的内存缓存。

 **便宜是硬要求**：「交易账号登录了没」那一项要连客户端、查一次资产，几秒钟且占用
账户串行槽。诊断页每 5 秒轮询一次的话，不缓存等于整天骚扰交易通道
⇒ `GET /v1/health` 读缓存，只有 `POST /v1/health/refresh` 才真去探。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from cn_broker_api.state.cached_health import CachedHealth


class HealthCache:
    """健康检查结果的内存缓存（理由见模块 docstring）。"""

    def __init__(self, ttl_seconds: int = 30) -> None:
        self.ttl = max(0, int(ttl_seconds))
        self._lock = threading.Lock()
        #: **按键分开存**：不同调用方问的账号/时刻不一样，共用一份缓存会让 A 读到
        #: "按 B 的参数判"的结论。共用一格是这类缓存最典型的一种静默错。
        self._entries: Dict[str, CachedHealth] = {}

    def get(self, key: str = "") -> Optional[CachedHealth]:
        with self._lock:
            return self._entries.get(key)

    def fresh(self, *, key: str = "",
              now: Optional[datetime] = None) -> Optional[CachedHealth]:
        e = self.get(key)
        if e is not None and e.age_seconds(now) <= self.ttl:
            return e
        return None

    def put(self, payload: Dict[str, Any], *, key: str = "",
            now: Optional[datetime] = None) -> CachedHealth:
        e = CachedHealth(payload=payload, at=now or datetime.now())
        with self._lock:
            self._entries[key] = e
            if len(self._entries) > 16:      # 键的基数很小，这只是个失控上限
                for k in list(self._entries)[:4]:
                    self._entries.pop(k, None)
        return e

"""`cred_source = "request"` 时的密码存放处：**只在内存，绝不落盘**。

与日闩刻意反向：**日闩怕丢掉，密码怕留下来。**"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Optional


@dataclass
class PasswordVault:
    """`cred_source = "request"` 时的密码存放处：**只在内存，绝不落盘**。

     按 (账户, 日期) 存：跨天自动失效，不需要额外的清理逻辑，也不会让昨天下发的密码
    在今天被悄悄复用。
    """

    _slots: Dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def put(self, account: str, password: str, *, day: Optional[date] = None) -> None:
        with self._lock:
            self._slots[account or ""] = ((day or date.today()).isoformat(), password)

    def get(self, account: str, *, day: Optional[date] = None) -> Optional[str]:
        d = (day or date.today()).isoformat()
        with self._lock:
            got = self._slots.get(account or "")
        if not got or got[0] != d:
            return None
        return got[1]

    def clear(self) -> None:
        with self._lock:
            self._slots.clear()

    def accounts_with_password(self, *, day: Optional[date] = None) -> list:
        """哪些账户手上有密码。**只报账户名，绝不报密码**——诊断页要显示这个。"""
        d = (day or date.today()).isoformat()
        with self._lock:
            return sorted(a for a, (day_s, _) in self._slots.items() if day_s == d)

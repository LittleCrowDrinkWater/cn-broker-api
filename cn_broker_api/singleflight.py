"""单飞：同一时刻只跑一趟那件事。第二个请求**不排队**，直接拿到正在跑的那个任务号
（REST 天然诱发重复调用：库重试、超时重发、监控周期打、浏览器刷新）。

放在 HTTP 层之外是因为**看门狗要用同一把**——让它反过来 import HTTP 层，依赖方向就错了。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple


class SingleFlight:
    """同一时刻只跑一趟。第二个请求**不排队**，拿到正在跑的那个任务号。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Optional[str] = None
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def start(self) -> Tuple[str, bool]:
        """返回 (任务号, 是不是本次新开的)。"""
        with self._lock:
            if self._current:
                return self._current, False
            job_id = uuid.uuid4().hex[:12]
            self._current = job_id
            self._jobs[job_id] = {"id": job_id, "state": "running",
                                  "started_at": datetime.now().isoformat(timespec="seconds")}
            return job_id, True

    def finish(self, job_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id) or {"id": job_id}
            job.update(payload)
            job["state"] = "done"
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            self._jobs[job_id] = job
            if self._current == job_id:
                self._current = None
            # 只留最近若干条：这是个运维端点，不是审计日志。
            if len(self._jobs) > 32:
                for k in sorted(self._jobs, key=lambda x: self._jobs[x].get("started_at", ""))[:8]:
                    self._jobs.pop(k, None)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._jobs.get(job_id)

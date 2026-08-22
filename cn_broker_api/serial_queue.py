"""按账户分组的串行队列。

到客户端的连接是**进程级单条**，所以所有调用必须排队。叫号按账户成批：同一个账户排着的
调用全做完，再切下一个账户——不是先来先服务，因为换账户要付「断开 0.5 秒 + 重连秒级」，
两个账户交替进来就是几十轮来回。

⇒ 「批」这个概念在调用方那侧直接消失，不需要跨进程租约。
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from cn_broker_api.queue_timeout import QueueTimeout

logger = logging.getLogger(__name__)

#: 默认等多久。要比厂商单次调用的超时（下单 30 秒）宽裕，否则排在后面的请求会先超时。
DEFAULT_WAIT_SECONDS = 120.0


class _Task:
    __slots__ = ("fn", "what", "done", "value", "error", "abandoned", "started")

    def __init__(self, fn: Callable[[], Any], what: str) -> None:
        self.fn = fn
        self.what = what
        self.done = threading.Event()
        self.value: Any = None
        self.error: Optional[BaseException] = None
        self.abandoned = False
        self.started = False


class AccountSerialQueue:
    """一条工作线程按账户成批叫号。`submit` 阻塞调用线程直到做完。"""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._queues: Dict[str, Deque[_Task]] = {}
        #: 有活在排的账户，**按账户第一次排上队的顺序**。分组就靠它：队头那个账户排着的
        #: 调用全做完才轮到下一个，而不是按单个调用的先后。
        self._waiting: List[str] = []
        self._thread: Optional[threading.Thread] = None

    def submit(self, account: str, fn: Callable[[], Any], *, what: str = "",
               wait_seconds: float = DEFAULT_WAIT_SECONDS) -> Any:
        """把一次调用排进 `account` 的队列，等它做完并返回结果（异常原样抛出）。

        Raises:
            QueueTimeout: 等到 `wait_seconds` 还没轮到（下单时＝状态未知）。
        """
        task = _Task(fn, what or "(未命名)")
        with self._cv:
            self._ensure_worker()
            self._queues.setdefault(account, deque()).append(task)
            if account not in self._waiting:
                self._waiting.append(account)
            self._cv.notify()
        if task.done.wait(timeout=wait_seconds):
            if task.error is not None:
                raise task.error
            return task.value
        with self._cv:
            # 还没开跑就作废，别在调用方放弃之后再发出去——一笔没人知道的委托比一次明确的
            # 超时糟得多。已经开跑的拦不住，只能记红。
            task.abandoned = not task.started
            still_running = task.started
        if still_running:
            logger.error("[queue] %s 已经在执行、调用方却已超时放弃 ⇒ 结果无人接收，"
                         "必须去柜台重新观测", task.what)
        raise QueueTimeout(f"排队 {wait_seconds:.0f} 秒仍未轮到（{task.what}）")

    def _ensure_worker(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="broker-serial",
                                            daemon=True)
            self._thread.start()

    def _next_locked(self) -> Optional[_Task]:
        """下一个要做的活：队头账户的队头调用。空掉的账户顺手摘掉。"""
        while self._waiting:
            acc = self._waiting[0]
            if self._queues.get(acc):
                return self._queues[acc].popleft()
            self._waiting.pop(0)
            self._queues.pop(acc, None)
        return None

    def _run(self) -> None:
        while True:
            with self._cv:
                task = self._next_locked()
                while task is None:
                    self._cv.wait()
                    task = self._next_locked()
                if task.abandoned:
                    continue
                task.started = True
            try:
                task.value = task.fn()
            except BaseException as e:  # noqa: BLE001 — 原样带回调用线程
                task.error = e
            finally:
                task.done.set()

"""按账户分组的串行队列。"""
from __future__ import annotations

import threading
import time

import pytest

from cn_broker_api.queue_timeout import QueueTimeout
from cn_broker_api.serial_queue import AccountSerialQueue


def _wait_until(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


def test_same_account_runs_in_order():
    q = AccountSerialQueue()
    seen = []
    for i in range(5):
        q.submit("A", lambda i=i: seen.append(i))
    assert seen == [0, 1, 2, 3, 4]


def test_result_and_exception_come_back_to_the_caller():
    q = AccountSerialQueue()
    assert q.submit("A", lambda: 7) == 7
    with pytest.raises(ZeroDivisionError):
        q.submit("A", lambda: 1 / 0)


def test_one_account_is_served_to_the_end_before_switching():
    """⭐ 不是先来先服务：换账户要付「断开 0.5 秒 + 重连秒级」，两个账户交替进来就是
    几十轮来回。所以同一个账户排着的调用要全做完再切。

    用私有状态等"两笔都排上了"，是为了让这条断言不依赖线程调度的运气。
    """
    q = AccountSerialQueue()
    seen = []
    hold = threading.Event()

    def first():
        seen.append("A1")
        hold.wait(2.0)

    threading.Thread(target=lambda: q.submit("A", first), daemon=True).start()
    assert _wait_until(lambda: seen == ["A1"])
    threading.Thread(target=lambda: q.submit("B", lambda: seen.append("B1")),
                     daemon=True).start()
    threading.Thread(target=lambda: q.submit("A", lambda: seen.append("A2")),
                     daemon=True).start()
    assert _wait_until(lambda: len(q._queues.get("A", ())) == 1
                       and len(q._queues.get("B", ())) == 1)
    hold.set()
    assert _wait_until(lambda: len(seen) == 3)
    assert seen == ["A1", "A2", "B1"]


def test_timeout_raises_and_drops_the_task_that_had_not_started():
    """🔴 调用方已经放弃之后再把委托发出去，就是一笔没人知道的委托——比一次明确的超时
    糟得多。所以还没开跑的任务要作废。"""
    q = AccountSerialQueue()
    ran = []
    hold, started = threading.Event(), threading.Event()

    def blocking():
        started.set()
        hold.wait(2.0)

    threading.Thread(target=lambda: q.submit("A", blocking), daemon=True).start()
    assert started.wait(2.0)
    with pytest.raises(QueueTimeout):
        q.submit("A", lambda: ran.append("late"), wait_seconds=0.05)
    hold.set()
    assert not _wait_until(lambda: ran == ["late"], timeout=0.3)

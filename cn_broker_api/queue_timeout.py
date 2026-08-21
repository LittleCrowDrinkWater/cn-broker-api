"""排队等超时。"""
from __future__ import annotations


class QueueTimeout(TimeoutError):
    """排到超时仍没轮到。

    对下单来说这是**状态未知**（可能已经在跑了），调用方必须重新观测柜台，
    不能当成「没报出去」。
    """

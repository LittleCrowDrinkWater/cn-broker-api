"""这个进程今天的拉起次数用完了。"""
from __future__ import annotations


class StartBudgetUsedUp(Exception):
    """这个进程今天的拉起次数用完了。**不是错误，是闸门生效了**——
    表现该是「诊断页上写着用完了」，不是一条异常。"""

"""驱动侧的失败。"""
from __future__ import annotations



class DriverError(Exception):
    """驱动侧的失败。**传输层与业务层的区分留给各驱动自己**——在这一层强行统一，
    查询类就再也没法把「查不到」和「真的没有」分开。"""

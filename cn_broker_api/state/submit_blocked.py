"""今天这个账户的密码提交次数已经用完。"""
from __future__ import annotations


class SubmitBlocked(Exception):
    """今天这个账户的密码提交次数已经用完。**不是错误，是闸门生效了**——
    调用方该把它翻译成一句人话让人自己去登，而不是当异常上报。"""

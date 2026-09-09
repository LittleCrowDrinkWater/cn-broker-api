"""交易客户端会话的稳定状态值。"""
from __future__ import annotations

from enum import Enum


class SessionState(str, Enum):
    """调用方据此决定继续业务、触发登录还是请求人工处理。"""

    READY = "READY"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    LOGIN_IN_PROGRESS = "LOGIN_IN_PROGRESS"
    MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"
    LOGIN_LOCKED = "LOGIN_LOCKED"
    CHANNEL_UNAVAILABLE = "CHANNEL_UNAVAILABLE"


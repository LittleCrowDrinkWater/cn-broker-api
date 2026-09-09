"""交易账户明确未登录。"""
from __future__ import annotations

from cn_broker_api.drivers.driver_error import DriverError
from cn_broker_api.drivers.session_state import SessionState


class BrokerLoginRequired(DriverError):
    """已有确切未登录证据，调用方可以安全转去调用登录接口。"""

    session_state = SessionState.LOGIN_REQUIRED.value


"""柜台明确拒单。"""
from __future__ import annotations

from cn_broker_api.drivers.driver_error import DriverError


class OrderRejected(DriverError):
    """定性拒单：这笔没报出去，原因已知。线上表达 `409`。"""

    def __init__(self, message: str, *, broker_message: str = "") -> None:
        super().__init__(message)
        self.broker_message = broker_message or message

"""要的能力这个驱动没有。**明确失败，不降级。**"""
from __future__ import annotations

from cn_broker_api.drivers.driver_error import DriverError


class CapabilityMissing(DriverError):
    """要的能力这个驱动没有。**明确失败，不降级。**"""

    def __init__(self, capability: str, driver: str) -> None:
        super().__init__(
            f"驱动 {driver} 不具备能力 {capability!r} ⇒ 拒绝这次调用。"
            f"静默降级的表现是「那天等于没调仓」，所以这里宁可报错")
        self.capability = capability
        self.driver = driver

"""委托推到客户端等人确认——**不是拒单**。"""
from __future__ import annotations

from cn_broker_api.drivers.driver_error import DriverError


class OrderPendingConfirm(DriverError):
    """结果还没定：厂商三态里的 `1=待用户确认`。线上表达 `202`。

    🔴 2026-08-19 把它当拒单处置，三笔委托被记成 failed/expired，而柜台上已经全部成交
    8600 股 ⇒ 卖券还款只扫 bought，那笔融资买入差点负债过夜。调用方唯一能做的事是
    **过一会儿重新观测柜台**。

    ⚠️ 本服务**不自己消化**这个中间态（不等自动确认补丁点掉再回报）：那是行为变更，
    而 09:23 那个窗口只有两分钟，改等待逻辑要重新量时序。
    """

    def __init__(self, message: str, *, broker_message: str = "") -> None:
        super().__init__(message)
        self.broker_message = broker_message or message

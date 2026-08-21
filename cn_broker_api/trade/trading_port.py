"""交易动词的接口。形状与调用方的 `TradingPort` 对齐，勿自创。

查询类一律返回三态字典（见 `wire`），**判不了就抛 `QueryUnavailable`**，绝不折成空表。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Sequence

from cn_broker_api.trade.credit_kind import CreditOrderKind


class Trading(Protocol):
    """一个账户上的交易与查询。实例由驱动按 (账号, 类别) 给出。"""

    def create_order(self, *, symbol: str, side: str, size: int,
                     price: Optional[float] = None, order_type: str = "limit",
                     client_order_id: Optional[str] = None,
                     credit_kind: Optional[CreditOrderKind] = None,
                     notify: Optional[int] = None) -> Dict[str, Any]:
        """报一笔限价单，返回 `wire.order_row`。

        Raises:
            OrderPendingConfirm: 推给客户端等人确认（**不是拒单**）。
            OrderRejected: 定性拒单。
            DriverError: 通道不可用。
        """
        ...

    def cancel_order(self, *, symbol: str, order_id: str) -> Dict[str, Any]:
        """撤单，返回 `{"canceled": bool, "order": row|None, "reason": str}`。

        整段「提交撤单 → 重查 → 撤完再读一次、按事实定终态」都在这里面。
        """
        ...

    def get_order(self, *, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        """单笔委托；`None` ＝不在当日委托簿里。判不了抛 `QueryUnavailable`。"""
        ...

    def get_orders(self) -> List[Dict[str, Any]]:
        """当日全部委托。判不了抛 `QueryUnavailable`。"""
        ...

    def get_positions(self) -> List[Dict[str, Any]]:
        """持仓。判不了抛 `QueryUnavailable`。"""
        ...

    def get_sellable(self) -> Dict[str, str]:
        """`{代码: 可卖股数}`。判不了抛 `QueryUnavailable`。"""
        ...

    def get_account(self) -> Optional[Dict[str, Any]]:
        """账户资产；`None` ＝一个资金字段都没有（取不到 ≠ 权益是 0）。"""
        ...

    def quotes(self, codes: Sequence[str]) -> List[Dict[str, Any]]:
        """快照。取不到的代码不出现在结果里。"""
        ...

    def instrument(self, code: str) -> Optional[Dict[str, Any]]:
        """标的静态信息（含 `margin_target` 三态）；`None` ＝查不到。"""
        ...

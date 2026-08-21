"""纸面驱动的交易那一半：只回答「接口通不通」。

**不模拟撮合、不模拟成交、不模拟拒单。** 真正要验的东西（撤单与成交赛跑、委托中间态、
查询三态）只有在真柜台上才有意义，假件在这些地方越像越危险。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from cn_broker_api.symbols import to_tq_code
from cn_broker_api.trade.credit_kind import CreditOrderKind
from cn_broker_api.trade.wire import (CANCELED, LIVE, account_row, order_row,
                                      quote_row)


class PaperTrading:
    """内存里的一本委托簿，收下就算报出去了。"""

    def __init__(self) -> None:
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._seq = 0

    def create_order(self, *, symbol: str, side: str, size: int,
                     price: Optional[float] = None, order_type: str = "limit",
                     client_order_id: Optional[str] = None,
                     credit_kind: Optional[CreditOrderKind] = None,
                     notify: Optional[int] = None) -> Dict[str, Any]:
        self._seq += 1
        row = order_row(order_id=f"paper-{self._seq}", client_order_id=client_order_id,
                        symbol=to_tq_code(symbol), side=str(side).lower(), status=LIVE,
                        size=int(size), price=price, order_type=order_type)
        self._orders[row["order_id"]] = row
        return row

    def cancel_order(self, *, symbol: str, order_id: str) -> Dict[str, Any]:
        row = self._orders.get(str(order_id))
        if row is None:
            return {"canceled": True, "order": None, "reason": "不在簿"}
        row["status"] = CANCELED
        return {"canceled": True, "order": row, "reason": "纸面驱动：直接记已撤"}

    def get_order(self, *, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        return self._orders.get(str(order_id))

    def get_orders(self) -> List[Dict[str, Any]]:
        return list(self._orders.values())

    def get_positions(self) -> List[Dict[str, Any]]:
        """恒空。⭐ 空表在这里是**真的空**（纸面驱动没有持仓），不是"判不了"——
        两者的区别正是这套契约最要紧的一位。"""
        return []

    def get_sellable(self) -> Dict[str, Optional[str]]:
        return {}

    def get_account(self) -> Optional[Dict[str, Any]]:
        """一个全零的账户。⭐ 不返回 `None`：`None` 的语义是「取不到」，
        而纸面驱动是真的知道自己没有钱。"""
        return account_row(total_equity=0, total_available=0)

    def quotes(self, codes: Sequence[str], *,
               depth: bool = False) -> List[Dict[str, Any]]:
        return [quote_row(symbol=to_tq_code(c), last=0, prev_close=0) for c in codes]

    def instrument(self, code: str) -> Optional[Dict[str, Any]]:
        """`margin_target` 恒 `None`＝判不了。纸面驱动不知道两融名单，
        谎报 True 会让调用方拿一份假名单去下融资单。"""
        return {"symbol": to_tq_code(code), "name": "", "margin_target": None}

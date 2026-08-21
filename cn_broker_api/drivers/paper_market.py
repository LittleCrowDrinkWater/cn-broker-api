"""纸面驱动的行情那一半：形状对，数据空。

⭐ 空表在这里是**真的空**（纸面驱动确实没有行情），不是"判不了"——这两件事的区别正是
这套契约最要紧的一位，所以假件也不许含糊。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from cn_broker_api.drivers.tdxquant.market import KLINE_UNITS
from cn_broker_api.symbols import to_tq_code
from cn_broker_api.trade.wire import quote_row


class PaperMarketData:
    """不连任何东西的行情源。"""

    def klines(self, codes: Sequence[str], *, period: str = "1d", count: int = 60,
               refresh: bool = False,
               dividend_type: Optional[str] = None) -> Dict[str, Any]:
        return {"period": period, "units": dict(KLINE_UNITS), "rows": [],
                "totals": {to_tq_code(c): 0 for c in codes}, "has_more": False}

    def quotes(self, codes: Sequence[str], *, depth: bool = False) -> List[Dict[str, Any]]:
        return [quote_row(symbol=to_tq_code(c), last=0, prev_close=0) for c in codes]

    def prices(self, codes: Sequence[str]) -> List[Dict[str, Any]]:
        return [{"symbol": to_tq_code(c), "last": "0", "prev_close": "0", "volume": "0"}
                for c in codes]

    def limit_status(self, codes: Sequence[str]) -> List[Dict[str, Any]]:
        return []

    def dividends(self, code: str, *, start: str = "", end: str = "") -> List[Dict[str, Any]]:
        return []

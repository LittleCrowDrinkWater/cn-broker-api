"""线上表达：查询三态与各类行的字段。

两条纪律：

① **查询三态**：`{"known": true, "rows": [...]}` 与 `{"known": false, "reason": "..."}`
   都是 200；只有传输层失败才走非 2xx。「查不到 ≠ 真的空」这一位必须显式带在响应里。
② **数值一律走字符串**。调用方那侧是 Decimal 账本，`Decimal(str)` 精确而 `Decimal(float)`
   会把二进制误差烘进去。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: 委托状态取值，与调用方的 `OrderStatus` 一字不差。
LIVE, PARTIALLY_FILLED, FILLED, CANCELED = "live", "partially_filled", "filled", "canceled"


def num(value: Any) -> Optional[str]:
    """数值 → 字符串（`None` 保持 `None`）。"""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def known(rows: Any) -> Dict[str, Any]:
    return {"known": True, "rows": rows}


def unknown(reason: str) -> Dict[str, Any]:
    return {"known": False, "reason": reason}


def order_row(*, order_id: Optional[str], symbol: str, side: str, status: str,
              size: Any, price: Any = None, filled_size: Any = 0,
              avg_fill_price: Any = 0, client_order_id: Optional[str] = None,
              order_type: str = "limit") -> Dict[str, Any]:
    return {"order_id": (None if order_id is None else str(order_id)),
            "client_order_id": client_order_id, "symbol": symbol, "side": side,
            "order_type": order_type, "status": status,
            "size": num(size), "price": num(price),
            "filled_size": num(filled_size), "avg_fill_price": num(avg_fill_price)}


def position_row(*, symbol: str, size: Any, avg_price: Any,
                 mark_price: Any = None, unrealized_pnl: Any = 0,
                 sellable: Any = None) -> Dict[str, Any]:
    """一行持仓。

    `mark_price` / `unrealized_pnl` 在这条通道上基本恒空——券商不返回现价与市值，
    调用方自己用行情补算。`sellable` 与 `/v1/positions/sellable` 同源（一次查询就都有了），
    那个端点单独留着是因为调用方的卖出腿只要这一列。
    """
    return {"symbol": symbol, "size": num(size), "avg_price": num(avg_price),
            "mark_price": num(mark_price), "unrealized_pnl": num(unrealized_pnl),
            "sellable": num(sellable)}


def account_row(*, total_equity: Any, total_available: Any, total_margin: Any = 0,
                total_unrealized_pnl: Any = 0, currency: str = "CNY",
                cash_balance: Any = 0, frozen: Any = 0) -> Dict[str, Any]:
    """账户资产。**信用账户口径与普通账户相同、不体现负债**：负债字段名在这套 API 里
    实测不可见（80 个函数只有 3 个账户查询，签名里没有两融参数），不猜键名。"""
    return {"total_equity": num(total_equity), "total_available": num(total_available),
            "total_margin": num(total_margin),
            "total_unrealized_pnl": num(total_unrealized_pnl),
            "balance": {"currency": currency, "cash_balance": num(cash_balance),
                        "available": num(total_available), "frozen": num(frozen)}}


def quote_row(*, symbol: str, last: Any, prev_close: Any,
              bid1: Any = None, ask1: Any = None) -> Dict[str, Any]:
    """一行快照。**只给实测过的四个字段**（Now / LastClose / Buyp / Sellp）——
    盘口挂单量的键名没实测过，猜键名的表现是一串恒为 0 的数。"""
    return {"symbol": symbol, "last": num(last), "prev_close": num(prev_close),
            "bid1": num(bid1), "ask1": num(ask1)}


def kline_row(*, symbol: str, at: str, open_: Any, high: Any, low: Any, close: Any,
             volume: Any, amount: Any) -> Dict[str, Any]:
    """一根 K 线。`at` 是 `YYYYMMDD`（日及以上）或 `YYYYMMDDHHMMSS`（分钟级）。

    单位随响应的 `units` 一起给（K 线的成交量是**股**、成交额是**万元**），
    因为同一个客户端里两个接口的单位就不一样。
    """
    return {"symbol": symbol, "datetime": at, "open": num(open_), "high": num(high),
            "low": num(low), "close": num(close), "volume": num(volume),
            "amount": num(amount)}


def limit_status_row(*, symbol: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """封板状态。**厂商的键名原样转发**，只补 `symbol`、丢掉那个坏的 `Code`。

     实测厂商的 `Code` 字段是截断的（`".SZ"`）⇒ 代码只认外层那个键。
     各字段的语义厂商没写，本服务不猜、不改名：猜错一个键的代价是把「没封板」
    读成「封住了」。
    """
    return {"symbol": symbol,
            **{k: (None if v is None else str(v)) for k, v in fields.items()
               if k not in ("Code", "ErrorId")}}


def dividend_row(*, symbol: str, date: str, kind: str,
                 cells: List[Any]) -> Dict[str, Any]:
    """一次除权除息。四个数值字段的列序抄自厂商源码，**单位未实测**。"""
    def cell(i: int):
        return num(cells[i]) if i < len(cells) else None

    return {"symbol": symbol, "date": date, "type": kind,
            "bonus": cell(0), "allot_price": cell(1),
            "share_bonus": cell(2), "allotment": cell(3)}


def rows_of(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从三态响应里取行（给用例用）。"""
    return list(payload.get("rows") or [])

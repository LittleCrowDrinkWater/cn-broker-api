"""客户端那侧的行情与静态数据。**不需要交易账户句柄**，所以交易没登也能用。

走的是客户端自己开的那个 JSON-RPC 端口（`McpClient`），不碰 `tqcenter` 模块，
也不取账户句柄——实测这些函数都不认账户。

## 四条实测出来的口径（2026-08-22 真机逐个打过）

① **区间取 K 线不管用**：给了 `start_time`/`end_time` 只回总数（`KlineTotal`）、行是空的，
   猜过三种分页参数名都没用 ⇒ 只暴露「最近 N 根」，**这条路不是历史回补的路**。
② **分钟线不刷缓存就是空的**：`refresh_kline` 之后同一个请求立刻有数 ⇒ 端点带 `refresh` 开关。
③ **单位在同一个客户端里就不统一**：K 线 `Volume` 是**股**、`Amount` 是**万元**；
   而 `get_pricevol`/快照的 `Volume` 是**手**。⇒ 每个响应自带 `units`，别让调用方去猜。
④ **`ForwardFactor` 实测恒 `0.000000`** ⇒ 别拿它当复权因子。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from cn_broker_api.drivers.driver_error import DriverError
from cn_broker_api.drivers.tdxquant.mcp import McpClient, TransportError
from cn_broker_api.symbols import to_tq_code
from cn_broker_api.trade.query_unavailable import QueryUnavailable
from cn_broker_api.trade.wire import dividend_row, kline_row, limit_status_row, quote_row

logger = logging.getLogger(__name__)

#: 厂商认的周期。**照抄它那份清单**，别自己加别名——写歪了它回的是 `error: -5`。
PERIODS = ("1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1mon", "45d", "1q", "1y")

#: 一次最多几个代码。厂商那侧一页 100 个（`stock_page_size`），超了要翻页；
#: 而这些调用与报单共用同一条串行连接 ⇒ 不给翻页，让调用方自己分批。
MAX_CODES = 100

#: 一次最多几根 K 线。纯粹是护栏：这条路只能取最近 N 根，要更多也没有意义。
MAX_COUNT = 2000

#: K 线与快照的单位，随响应一起给出（见模块 docstring ③）。
KLINE_UNITS = {"volume": "shares", "amount": "wan_yuan"}
TICK_UNITS = {"volume": "lots", "amount": "wan_yuan"}


def validate_klines(codes: Sequence[str], period: str, count: int):
    """把 K 线请求校成 (代码表, 周期, 根数)。

    ⭐ 校验放在**这一个函数**里，路由与驱动都调它：只在驱动里校，纸面驱动那条路就没人拦；
    只在路由里校，直接调驱动的脚本就没人拦。两处各写一份，数字迟早分叉。
    """
    period = str(period).strip().lower()
    if period not in PERIODS:
        raise ValueError(f"period 只能是 {list(PERIODS)}，收到 {period!r}")
    count = int(count)
    if not 1 <= count <= MAX_COUNT:
        raise ValueError(f"count 要在 1~{MAX_COUNT} 之间，收到 {count}")
    return normalize_codes(codes), period, count


def normalize_codes(codes: Sequence[str]) -> List[str]:
    if not codes:
        raise ValueError("要给至少一个代码")
    if len(codes) > MAX_CODES:
        raise ValueError(f"一次最多 {MAX_CODES} 个代码，收到 {len(codes)} 个")
    return [to_tq_code(c) for c in codes]


class TdxQuantMarketData:
    """行情与静态数据。每个方法一次厂商调用，判不了一律抛 `QueryUnavailable`。"""

    def __init__(self, client: McpClient) -> None:
        self._client = client

    def _call(self, method: str, params: Dict[str, Any], *,
              timeout: float = 30.0) -> Dict[str, Any]:
        try:
            return self._client.call(method, params, timeout=timeout)
        except TransportError as e:
            raise DriverError(str(e)) from e

    _codes = staticmethod(normalize_codes)

    # ---- K 线 ----
    def klines(self, codes: Sequence[str], *, period: str = "1d", count: int = 60,
               refresh: bool = False,
               dividend_type: Optional[str] = None) -> Dict[str, Any]:
        """**最近 `count` 根** K 线。区间参数不提供，理由见模块 docstring ①。

        `refresh=True` 先让客户端刷一次该周期的缓存——**分钟线基本必须刷**（②）。
        `dividend_type` 原样转给厂商，本服务不解释它。
        """
        tq_codes, period, count = validate_klines(codes, period, count)

        if refresh:
            self._call("refresh_kline", {"stock_list": tq_codes, "period": period},
                       timeout=60.0)
        params: Dict[str, Any] = {"stock_list": tq_codes, "period": period, "count": count}
        if dividend_type:
            params["dividend_type"] = str(dividend_type)
        res = self._call("get_market_data", params, timeout=60.0)
        if str(res.get("ErrorId", "0")) != "0":
            raise QueryUnavailable(f"取 K 线失败：{res.get('Error') or res}")

        value = res.get("Value")
        if not isinstance(value, dict):
            raise QueryUnavailable("K 线返回体里没有 Value ⇒ 判不了")
        rows: List[Dict[str, Any]] = []
        for code, cols in value.items():
            if isinstance(cols, dict):
                rows.extend(_bars(code, cols))
        return {"period": period, "units": dict(KLINE_UNITS), "rows": rows,
                "totals": {k: int(v) for k, v in (res.get("KlineTotal") or {}).items()
                           if str(v).lstrip("-").isdigit()},
                "has_more": bool(res.get("has_more"))}

    # ---- 快照与批量报价 ----
    def quotes(self, codes: Sequence[str], *, depth: bool = False) -> List[Dict[str, Any]]:
        """逐个代码取快照（**厂商没有批量快照**）。`depth=True` 带上五档盘口。

        只要价格和成交量时用 `prices()`：那个是一次调用拿一批。
        """
        out = []
        for code in self._codes(codes):
            res = self._call("get_market_snapshot", {"stock_code": code})
            if str(res.get("ErrorId", "0")) != "0":
                continue                 # 判不了 != 没有这只票 ⇒ 不给一行假快照
            out.append(snapshot_row(code, res, depth=depth))
        return out

    def prices(self, codes: Sequence[str]) -> List[Dict[str, Any]]:
        """一次调用拿一批的现价 / 昨收 / 成交量。⚠️ 成交量单位是**手**（K 线那边是股）。"""
        res = self._call("get_pricevol", {"stock_list": self._codes(codes)})
        if str(res.get("ErrorId", "0")) != "0":
            raise QueryUnavailable(f"取批量报价失败：{res.get('Error') or res}")
        value = res.get("Value")
        if not isinstance(value, dict):
            raise QueryUnavailable("批量报价返回体里没有 Value ⇒ 判不了")
        return [{"symbol": code, "last": _s(row, "Now"),
                 "prev_close": _s(row, "LastClose"), "volume": _s(row, "Volume")}
                for code, row in value.items() if isinstance(row, dict)]

    # ---- 涨跌停封板状态 ----
    def limit_status(self, codes: Sequence[str]) -> List[Dict[str, Any]]:
        """封板状态：封单量、首次/最后封板时刻、开板次数。

        ⚠️ 它**不给涨跌停价格**（那还是要按板别与昨收自己算）。
        ⚠️ 字段语义厂商没写，本服务**原样转发厂商的键名**，不猜、不改名——
        猜错一个键的代价是把一个"没封板"读成"封住了"。
        """
        res = self._call("get_zdt_data", {"stock_list": self._codes(codes)}, timeout=60.0)
        if str(res.get("ErrorId", "0")) != "0":
            raise QueryUnavailable(f"取涨跌停数据失败：{res.get('Error') or res}")
        value = res.get("Value")
        if not isinstance(value, dict):
            raise QueryUnavailable("涨跌停返回体里没有 Value ⇒ 判不了")
        return [limit_status_row(symbol=code, fields=row)
                for code, row in value.items() if isinstance(row, dict)]

    # ---- 除权除息 ----
    def dividends(self, code: str, *, start: str = "", end: str = "") -> List[Dict[str, Any]]:
        """除权除息事件。

        ⚠️ **日期区间由本服务过滤**：厂商源码里写着「C接口的时间没有实际作用，返回的是所有
        权息数据」——它那侧的过滤是在 Python 层做的，而我们不走它那层。
        ⚠️ 四个数值字段照抄厂商源码里的列名（Bonus / AllotPrice / ShareBonus / Allotment），
        **单位没有实测过** ⇒ 拿它算复权因子之前先与已有来源对一遍。
        """
        tq_code = to_tq_code(code)
        res = self._call("get_divid_factors", {"stock_code": tq_code}, timeout=30.0)
        if str(res.get("ErrorId", "0")) != "0":
            raise QueryUnavailable(f"取除权除息失败：{res.get('Error') or res}")
        dates = res.get("Date") or []
        types = res.get("Type") or []
        values = res.get("Value") or []
        rows = []
        for i, date in enumerate(dates):
            day = str(date)
            if (start and day < str(start)) or (end and day > str(end)):
                continue
            cells = values[i] if i < len(values) and isinstance(values[i], list) else []
            rows.append(dividend_row(symbol=tq_code, date=day,
                                     kind=str(types[i]) if i < len(types) else "",
                                     cells=cells))
        return rows


def _bars(code: str, cols: Dict[str, Any]) -> List[Dict[str, Any]]:
    """厂商给的是按字段并列的数组，转成一根一行。"""
    dates = cols.get("Date") or []
    times = cols.get("Time") or []
    out = []
    for i, date in enumerate(dates):
        clock = str(times[i]) if i < len(times) else "0"
        stamp = str(date) if clock in ("0", "000000", "") else f"{date}{int(clock):06d}"
        out.append(kline_row(symbol=code, at=stamp,
                             open_=_at(cols, "Open", i), high=_at(cols, "High", i),
                             low=_at(cols, "Low", i), close=_at(cols, "Close", i),
                             volume=_at(cols, "Volume", i), amount=_at(cols, "Amount", i)))
    return out


def snapshot_row(code: str, snap: Dict[str, Any], *,
                 depth: bool = False) -> Dict[str, Any]:
    """一行快照。**交易那侧也用这一份**——两处各写一遍，`depth` 的字段就会慢慢分叉。"""
    row = quote_row(symbol=code, last=_s(snap, "Now"), prev_close=_s(snap, "LastClose"),
                    bid1=_level(snap, "Buyp"), ask1=_level(snap, "Sellp"))
    if not depth:
        return row
    row.update(units=dict(TICK_UNITS),
               open=_s(snap, "Open"), high=_s(snap, "Max"), low=_s(snap, "Min"),
               average=_s(snap, "Average"), volume=_s(snap, "Volume"),
               amount=_s(snap, "Amount"), last_size=_s(snap, "NowVol"),
               trades=_s(snap, "ItemNum"), inside=_s(snap, "Inside"),
               outside=_s(snap, "Outside"),
               bids=_book(snap, "Buyp", "Buyv"), asks=_book(snap, "Sellp", "Sellv"))
    return row


def _book(snap: Dict[str, Any], price_key: str, size_key: str) -> List[Dict[str, Any]]:
    """五档。⭐ 封板时买一那档的挂单量就是封单量，这是要 depth 的主要原因。"""
    prices = snap.get(price_key)
    sizes = snap.get(size_key)
    if not isinstance(prices, (list, tuple)):
        return []
    out = []
    for i, px in enumerate(prices):
        size = sizes[i] if isinstance(sizes, (list, tuple)) and i < len(sizes) else None
        out.append({"price": _plain(px), "size": _plain(size)})
    return out


def _at(cols: Dict[str, Any], key: str, i: int):
    seq = cols.get(key)
    if isinstance(seq, (list, tuple)) and i < len(seq):
        return seq[i]
    return None


def _s(d: Dict[str, Any], key: str):
    return _plain(d.get(key))


def _plain(v):
    """厂商给的是字符串，原样带走——**别转 float 再转回来**，那是白烘一次误差。"""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    return None if v is None else str(v).strip()


def _level(snap: Dict[str, Any], key: str):
    return _plain(snap.get(key))

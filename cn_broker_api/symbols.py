"""代码归一：入口把任何写法都变成带后缀的 tqcenter 代码（`000761.SZ`）。

裸 6 位码在客户端那侧返回 `{"ErrorId": "2", "Error": "stock_code error:000761"}`，
不抛异常、不会自己暴露，所以归一必须在入口做掉。
"""
from __future__ import annotations

SH, SZ, BJ = "SH", "SZ", "BJ"


def symbol_key(symbol: str) -> str:
    """去市场后缀 + 补零到 6 位。"""
    s = str(symbol).strip().upper()
    if "." in s:
        s = s.split(".", 1)[0]
    return s.zfill(6) if s.isdigit() else s


def market_of(symbol: str) -> str:
    """裸代码 → 交易所归属 SH / SZ / BJ。

    口径与母项目 `backend/gateway/cn/board_rules.market_of` 一致，两处要一起改。
    """
    s = symbol_key(symbol)
    if s[:3] == "920":         # 北交所新号段，必须先于下面「9 → SH（老沪 B 股）」判
        return BJ
    c = s[:1]
    if c == "6":
        return SH
    if c in ("0", "3"):
        return SZ
    if c in ("4", "8"):
        return BJ
    return SH if c in ("5", "9") else SZ


def to_tq_code(symbol: str) -> str:
    """内部代码 → `600000.SH`。已带后缀的原样返回（大写）。"""
    s = str(symbol).strip().upper()
    if "." in s:
        return s
    if len(s) == 6 and s.isdigit():
        return f"{s}.{market_of(s)}"
    return s

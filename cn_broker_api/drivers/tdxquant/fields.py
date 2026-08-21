"""厂商 dict 的字段读取与状态判定。字段名全部实测过（2026-07-28 起）。"""
from __future__ import annotations

from typing import Any, Dict

from cn_broker_api.trade.wire import CANCELED, FILLED, LIVE, PARTIALLY_FILLED

#: 「已发送信号至客户端，待用户确认」那一态的文案。
PENDING_CONFIRM_MSG = "待用户确认"


def f(d: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    """取第一个存在且非空的键并转 float。多键是给不同客户端版本兜底的。"""
    for k in keys:
        if k in d and d[k] not in (None, "", "--"):
            try:
                return float(d[k])
            except (TypeError, ValueError):
                pass
    return float(default)


def s(d: Dict[str, Any], *keys: str, default: str = "") -> str:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return str(d[k])
    return default


def is_pending_confirm(res: Any) -> bool:
    """这次下单回执是不是「已推给客户端、等人确认」。

    ⚠️ **两个判据都要**：`Value` 在部分版本上是字符串、也可能整个字段缺失，
    那时只剩 `Msg` 认得出。
    """
    if not isinstance(res, dict):
        return False
    if PENDING_CONFIRM_MSG in str(res.get("Msg") or ""):
        return True
    return str(res.get("Value", "")).strip() == "1" and not res.get("Wtbh")


def order_status(o: Dict[str, Any]) -> str:
    """委托行 → 状态。

    以**成交量/委托量**为准而不看 `Status`：状态码含义随版本可能变，成交量是硬事实
    （实测 12 笔 Status=3 全判 filled、7 笔 Status=1 全判 live，与资金占用逐笔对得上）。
    """
    wt, cj = f(o, "WtVol"), f(o, "CjVol")
    if cj >= wt > 0:
        return FILLED
    if int(o.get("BSFlag", 0)) == -1 or int(o.get("Status", -99)) == 0:
        return PARTIALLY_FILLED if cj > 0 else CANCELED
    return PARTIALLY_FILLED if cj > 0 else LIVE


def order_side(o: Dict[str, Any]) -> str:
    """BSFlag 0 买 / 1 卖（-1 是已撤，方向此时不重要，按买回退）。"""
    return "sell" if int(o.get("BSFlag", 0)) == 1 else "buy"

"""厂商 dict 的字段读取与状态判定。字段名全部实测过（2026-07-28 起）。"""
from __future__ import annotations

from typing import Any, Dict, Optional

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

     **两个判据都要**：`Value` 在部分版本上是字符串、也可能整个字段缺失，
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


def order_time(o: Dict[str, Any]) -> Optional[str]:
    """委托行 → 报单时刻 `HHMMSS`；认不出格式返回 `None`。

    `Time` 是当日委托簿 11 个字段里唯一还带信息的一个（`Status`/`KPFlag`/`WTFS` 实测当天
    全是常量）。调用方拿它把「几小时前那笔」从认领候选里排除掉——委托类型在查询侧不可见，
    只按代码+委托量认领时，同一账户上另一条腿的同形委托会被认过来。

    🔴 **只认 5 位或 6 位数字**：`93000` 是小时的前导零被吞掉（补成 `093000`），而 4 位的
    `0923` 补零会变成 `000923`——把 09:23 读成 00:09，比没有这个字段更糟。分不清就给 `None`，
    调用方那侧的规矩是「缺时刻不排除」，退回原来的行为。
    """
    raw = s(o, "Time").replace(":", "").strip()
    if not raw.isdigit() or len(raw) not in (5, 6):
        return None
    hms = raw.zfill(6)
    hh, mm, ss = int(hms[:2]), int(hms[2:4]), int(hms[4:])
    if hh > 23 or mm > 59 or ss > 59:
        return None
    return hms

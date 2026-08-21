"""信用（两融）委托类型。"""
from __future__ import annotations

from enum import Enum


class CreditOrderKind(Enum):
    """值＝`tqconst` 上的属性名。

    存属性名而不是那串数字：编号只该有 tqconst 一份，厂商改号我们跟着变。
    """

    COLLATERAL_BUY = "CREDIT_BUY"        # 担保品买入（与普通买入同号 0）
    COLLATERAL_SELL = "CREDIT_SELL"      # 担保品卖出（与普通卖出同号 1）
    FIN_BUY = "CREDIT_FIN_BUY"           # 融资买入，产生负债
    SLO_SELL = "CREDIT_SLO_SELL"         # 融券卖出，需券源
    COV_BUY = "CREDIT_COV_BUY"           # 买券还券
    STK_REPAY = "CREDIT_STK_REPAY"       # 卖券还款（正T 的平仓腿）


#: 每种信用委托只有一个合法方向。报「卖券还款」却给 buy 是无意义的委托，
#: 与其让柜台回一句看不懂的柜台码，不如在发出前就拒。
CREDIT_KIND_SIDE = {
    CreditOrderKind.COLLATERAL_BUY: "buy",
    CreditOrderKind.FIN_BUY: "buy",
    CreditOrderKind.COV_BUY: "buy",
    CreditOrderKind.COLLATERAL_SELL: "sell",
    CreditOrderKind.SLO_SELL: "sell",
    CreditOrderKind.STK_REPAY: "sell",
}


def parse_credit_kind(raw) -> "CreditOrderKind | None":
    """请求里的 `credit_kind` 字符串 → 枚举。空 ⇒ None（报普通买卖）。

    Raises:
        ValueError: 认不出来的名字（**不静默忽略**：忽略的表现是"我以为报的是融资买入，
            其实报的是普通买入"，而两者的负债完全不同）。
    """
    if raw in (None, ""):
        return None
    name = str(raw).strip().upper()
    try:
        return CreditOrderKind[name]
    except KeyError:
        pass
    for kind in CreditOrderKind:
        if kind.value.upper() == name:
            return kind
    raise ValueError(f"认不出的 credit_kind {raw!r}，"
                     f"认得的是 {[k.name.lower() for k in CreditOrderKind]}")

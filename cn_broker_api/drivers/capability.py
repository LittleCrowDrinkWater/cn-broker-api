"""能力名。

## 为什么能力必须声明

不同驱动能做的事不一样：信用类委托（融资买入 / 卖券还款）是某些通道特有的，
一个"更精简的版本"很可能没有。调用方要的能力驱动没有 ⇒ **报错，不静默降级**。
静默降级在这个系统里的表现是「那天等于没调仓」——不报错、不留痕、事后才发现。"""
from __future__ import annotations



class Capability:
    """能力名。**字符串常量而不是枚举**：它要出现在 HTTP 响应里，跨进程传的是字符串，
    枚举只会在序列化边界上多一层转换。"""

    CREDIT_ORDER = "credit_order"          # 融资买入 / 卖券还款那一族
    CANCEL = "cancel"                      # 撤单
    BID_ASK_QUOTE = "bid_ask_quote"        # 快照带买卖盘口（不只最新价）
    SELLABLE_VOLUME = "sellable_volume"    # 可卖量单独可查
    DESKTOP_LOGIN = "desktop_login"        # 能替人把客户端登进去
    AUTOCONFIRM_PATCH = "autoconfirm_patch"  # 能保证「受理即真发出」

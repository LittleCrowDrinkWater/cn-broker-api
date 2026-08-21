"""能力名。缺了就报错，不静默降级——降级的表现是「那天等于没调仓」。"""
from __future__ import annotations



class Capability:
    """字符串常量而不是枚举：它要出现在 HTTP 响应里，枚举只多一层序列化转换。"""

    CREDIT_ORDER = "credit_order"          # 融资买入 / 卖券还款那一族
    CANCEL = "cancel"                      # 撤单
    BID_ASK_QUOTE = "bid_ask_quote"        # 快照带买卖盘口（不只最新价）
    SELLABLE_VOLUME = "sellable_volume"    # 可卖量单独可查
    MARKET_DATA = "market_data"            # K 线 / 快照 / 封板状态 / 除权除息（不要账户句柄）
    DESKTOP_LOGIN = "desktop_login"        # 能替人把客户端登进去
    DESKTOP_DIAG = "desktop_diag"          # 能收起窗口、能抓当时那个窗口的位图
    AUTOCONFIRM_PATCH = "autoconfirm_patch"  # 能保证「受理即真发出」

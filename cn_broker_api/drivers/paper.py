"""纸面驱动：不连任何东西。CI 靠它跨平台，联调靠它不必开客户端、不必等盘中。

⭐ 它还是「是契约错还是驱动错」的判别工具：同一个请求打两个驱动，一个对一个错就在驱动。

🔴 **不模拟撮合、不模拟成交、不模拟拒单**，只回答"接口通不通"。真正要验的那几件事
（撤单与成交赛跑、委托中间态、查询三态）只有在真柜台上才有意义，假件越像越危险。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from cn_broker_api.drivers.capability import Capability
from cn_broker_api.drivers.capability_missing import CapabilityMissing
from cn_broker_api.drivers.desktop_recipe import DesktopRecipe
from cn_broker_api.drivers.ensure_result import EnsureResult
from cn_broker_api.drivers.paper_market import PaperMarketData
from cn_broker_api.drivers.paper_trading import PaperTrading


class PaperDriver:
    """什么都不连的驱动。**所有检查项恒绿**，并在 detail 里明说自己是纸面的——
    ⭐ 不能让人在诊断页上看着一排绿以为客户端真的通了。"""

    name = "paper"

    def __init__(self) -> None:
        #: 调用了几次 ensure。给用例断言幂等用（真驱动那侧幂等由状态驱动的循环保证）。
        self.ensure_calls = 0
        self._books: Dict[str, PaperTrading] = {}

    def market(self) -> PaperMarketData:
        return PaperMarketData()

    def trading(self, *, account: str = "", account_type: str = "STOCK") -> PaperTrading:
        """按 (账号, 类别) 各一本委托簿——同一个账号在两个类别上是两条连接。"""
        return self._books.setdefault(f"{account_type}:{account}", PaperTrading())

    def capabilities(self) -> List[str]:
        """⭐ 刻意**不声明** `DESKTOP_LOGIN` / `AUTOCONFIRM_PATCH` / `CREDIT_ORDER`：
        谎报能力会让「能力声明」这个机制失去它存在的全部价值。"""
        return [Capability.CANCEL, Capability.BID_ASK_QUOTE, Capability.SELLABLE_VOLUME,
                Capability.MARKET_DATA]

    def desktop_recipe(self) -> DesktopRecipe:
        """空配方。⭐ 返回空的而不是抛错：看门狗要能问出「这个驱动没有进程要管」。"""
        return DesktopRecipe()

    def desktop_processes(self) -> Dict[str, bool]:
        return {}

    def start_desktop_process(self, name: str) -> None:
        """🔴 明确失败：调到这里就是漏了能力检查，静默不做会让人以为看门狗在工作。"""
        raise CapabilityMissing(Capability.DESKTOP_LOGIN, self.name)

    def health(self, *, account: str = "", account_type: str = "STOCK",
               need_times: Sequence[Tuple[int, int]] = ()) -> Dict[str, Any]:
        note = "纸面驱动，没有连接任何客户端"
        checks = [
            {"key": "transport", "name": "客户端进程", "ok": True, "warn": True,
             "detail": note},
            {"key": "account", "name": "交易账号登录", "ok": True, "warn": True,
             "detail": f"{note}；账号 {account or '(未指定)'} / {account_type}"},
            {"key": "quote", "name": "客户端行情", "ok": True, "warn": True, "detail": note},
            {"key": "autoconfirm", "name": "自动确认补丁", "ok": True, "warn": True,
             "detail": note},
        ]
        return {"ok": True, "checks": checks,
                "message": f"纸面驱动：四项均未真检查（{note}）"}

    def ensure_logged_in(self, *, password: Optional[str] = None,
                         account: str = "", account_type: str = "STOCK",
                         start: bool = True, minimize: bool = True,
                         wait_seconds: int = 240) -> EnsureResult:
        """恒成功、`acted=False`。🔴 **不碰 `password`**：为了逼真而存密码是纯风险。"""
        self.ensure_calls += 1
        return EnsureResult(ok=True, acted=False,
                            detail="纸面驱动：无需登录（也没有客户端可登）")

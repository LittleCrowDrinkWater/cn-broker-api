"""纸面驱动：不连任何东西。

## 它买到四样

1. **CI 能在 Linux 上跑** —— 真驱动是 Windows 专属（ctypes.windll + 窗口句柄），
   靠这个驱动，契约那一层的用例在任何平台都跑得过。
2. **调用方联调不用开客户端、不用等盘中** —— 这一条在实践中最省时间。
3. **跨仓库契约漂移的检测器** —— 调用方那侧的集成用例真的把本服务起起来打真 HTTP，
   比"两边各维护一份样例"可靠，因为它跑的就是真实现。
4. **出问题时能分清「是契约错还是驱动错」** —— 同一个请求打两个驱动，一个对一个错，
   问题就定位在驱动；两个都错，在契约或 HTTP 层。

## 刻意不做的

**不模拟撮合、不模拟成交、不模拟拒单。** 它只回答"接口通不通"。
一个"看起来很真"的假件会让人误以为验过了业务语义——而真正要验的东西（撤单与成交赛跑、
委托中间态、查询三态）只有在真柜台上才有意义。假件在这些地方越像，越危险。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from cn_broker_api.drivers.capability import Capability
from cn_broker_api.drivers.capability_missing import CapabilityMissing
from cn_broker_api.drivers.desktop_recipe import DesktopRecipe
from cn_broker_api.drivers.ensure_result import EnsureResult


class PaperDriver:
    """什么都不连的驱动。**所有检查项恒绿**，并在 detail 里明说自己是纸面的——
    ⭐ 不能让人在诊断页上看着一排绿以为客户端真的通了。"""

    name = "paper"

    def __init__(self) -> None:
        #: 调用了几次 ensure。给用例断言幂等用（真驱动那侧幂等由状态驱动的循环保证）。
        self.ensure_calls = 0

    def capabilities(self) -> List[str]:
        """⭐ 刻意**不声明** `DESKTOP_LOGIN` 与 `AUTOCONFIRM_PATCH`：纸面驱动确实做不到
        这两件事，谎报会让「能力声明」这个机制失去意义——而它存在的全部价值就是让调用方
        能 fail loud。"""
        return [Capability.CANCEL, Capability.BID_ASK_QUOTE, Capability.SELLABLE_VOLUME]

    def desktop_recipe(self) -> DesktopRecipe:
        """空配方：纸面驱动没有要看着的进程。⭐ 返回空的而不是抛错——
        看门狗那侧要能问出「这个驱动没有进程要管」，而不是撞一个异常。"""
        return DesktopRecipe()

    def desktop_processes(self) -> Dict[str, bool]:
        return {}

    def start_desktop_process(self, name: str) -> None:
        """🔴 明确失败。纸面驱动没声明 `DESKTOP_LOGIN`，谁真调到这里就是漏了能力检查——
        那种情况下静默什么都不做，会让人以为看门狗在工作。"""
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
        """恒成功、`acted=False`。

        🔴 **不碰 `password`**：纸面驱动收到密码也一个字符都不用，更不记下来。
        任何"为了逼真"而把密码存起来的做法都是纯风险、零收益。
        """
        self.ensure_calls += 1
        return EnsureResult(ok=True, acted=False,
                            detail="纸面驱动：无需登录（也没有客户端可登）")

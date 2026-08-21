"""驱动接口：唯一与厂商有关的那一层。HTTP 层与队列都不认识厂商。

对外契约与本接口同一个形状（都是 `TradingPort` 那套动词）⇒ 换驱动不动 HTTP 层。

两条纪律：
- **能力必须声明**，缺了就报错。静默降级在这个系统里的表现是「那天等于没调仓」。
- **配方是数据，机制是代码**（见 `desktop_recipe`）：换客户端版本多半是加一份配方。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from cn_broker_api.drivers.capability_missing import CapabilityMissing
from cn_broker_api.drivers.ensure_result import EnsureResult


class Driver(Protocol):
    """驱动要实现的那一组动词。形状与 `TradingPort` 对齐，勿自创。"""

    name: str

    def capabilities(self) -> List[str]:
        """这个驱动能做什么。进 `GET /v1/meta`，调用方据此 fail loud。"""
        ...

    def health(self, *, account: str = "", account_type: str = "STOCK",
               need_times: Sequence[Tuple[int, int]] = ()) -> Dict[str, Any]:
        """结构化检查项：`{"ok": bool, "checks": [{"key","name","ok","warn","detail"}]}`。

        有的检查项要连厂商、占串行槽 ⇒ 驱动不该假设自己只被调用一次（缓存在 HTTP 层）。
        """
        ...

    def trading(self, *, account: str = "", account_type: str = "STOCK") -> Any:
        """这个账户上的交易与查询对象（形状见 `trade.trading_port.Trading`）。

        账户是**参数**而不是驱动的构造状态：排队与连接切换都按它分组。
        """
        ...

    def market(self) -> Any:
        """行情与静态数据（形状见 `drivers.tdxquant.market`）。**不带账户维度**：
        这些调用不认账户，硬塞一个账户参数会让人以为行情也要交易登录。"""
        ...

    def ensure_logged_in(self, *, password: Optional[str] = None,
                         account: str = "", account_type: str = "STOCK",
                         start: bool = True, minimize: bool = True,
                         wait_seconds: int = 240) -> EnsureResult:
        """把状态从「没登」推到「登上了」。**必须幂等**。

        ⭐⭐ 状态驱动而非弹框驱动：客户端开了自动登录之后启动就直接登进行情、压根不弹框，
        「等登录框」那种写法会把「本来就好了」读成超时失败。
        🔴 **失败绝不重试**：密码错一次与错两次，后果差一个数量级。
        """
        ...


def require(driver: Driver, capability: str) -> None:
    """要一个能力，没有就明确失败。**调用点写一行，不要各自 if。**"""
    if capability not in driver.capabilities():
        raise CapabilityMissing(capability, getattr(driver, "name", "?"))

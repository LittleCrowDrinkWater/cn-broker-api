"""驱动接口：唯一与厂商有关的那一层。

## 为什么要这一层

现在下单走的是某一家的桌面客户端，将来可能换成更精简的版本、别家的量化终端、
某个 32 位的交易 DLL，或者一家真给 REST 的券商。HTTP 层不该知道这些。

```
HTTP 层 ── 账户亲和串行执行器 ── Driver ── 厂商
                                  ├── TdxQuantDriver
                                  ├── PaperDriver（不连任何东西）
                                  └── …
```

**对外契约与对内接口同一个形状**（都是 `TradingPort` 那套动词）⇒ 换驱动不动 HTTP 层。

## 能力必须声明，不能靠默认降级

不同驱动能做的事不一样：信用类委托（融资买入 / 卖券还款）是某些通道特有的，
一个"更精简的版本"很可能没有。调用方要的能力驱动没有 ⇒ **报错，不静默降级**。
静默降级在这个系统里的表现是「那天等于没调仓」——不报错、不留痕、事后才发现。

## 桌面监护：配方是数据，机制是代码

- **机制**（与厂商无关）：找窗口、分类是哪道门、填密码框、按颜色找按钮、量星号总宽、最小化。
- **配方**（每个驱动一份**数据**）：保证哪几个进程在跑、可执行文件相对路径、
  登录框怎么认、「登上了没有」用哪个调用验。

⇒ 换一个更精简的客户端版本，多半是**加一份配方**，不是写新代码。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from cn_broker_api.drivers.capability_missing import CapabilityMissing
from cn_broker_api.drivers.ensure_result import EnsureResult


class Driver(Protocol):
    """驱动要实现的那一组动词。形状与 `TradingPort` 对齐，勿自创。

    ⚠️ 本阶段只落地「监护 + 自检」这一半（搬家阶段不碰下单路径）。交易那几个动词的签名
    先不定死——等真要接管时，按母项目 `TradingPort` 的签名一次投影过来，
    免得先猜一版再改一版。
    """

    name: str

    def capabilities(self) -> List[str]:
        """这个驱动能做什么。进 `GET /v1/meta`，调用方据此 fail loud。"""
        ...

    def health(self, *, account: str = "", account_type: str = "STOCK",
               need_times: Sequence[Tuple[int, int]] = ()) -> Dict[str, Any]:
        """结构化检查项：`{"ok": bool, "checks": [{"key","name","ok","warn","detail"}]}`。

        🔴 **必须便宜或者可缓存**：有的检查项要连厂商、占用串行槽。缓存策略在 HTTP 层，
        但驱动这一侧不该假设自己只被调用一次。
        """
        ...

    def ensure_logged_in(self, *, password: Optional[str] = None,
                         account: str = "", account_type: str = "STOCK",
                         start: bool = True, minimize: bool = True,
                         wait_seconds: int = 240) -> EnsureResult:
        """把状态从「没登」推到「登上了」。**必须幂等**。

        ⭐⭐ 状态驱动，不是弹框驱动：循环是「已登上？→ 有框？→ 处理」。客户端开了自动登录
        之后启动就直接登进行情、压根不弹框，而"等登录框"那种写法会把「本来就好了」读成超时
        失败。幂等 ＝ 定时任务可以放心反复打。

        🔴 **失败绝不重试**。密码这件事上，"多试一次"正是把"填错一次"变成一个未知后果的
        那一步。失败就如实返回，让上层去告警。
        """
        ...


def require(driver: Driver, capability: str) -> None:
    """要一个能力，没有就明确失败。**调用点写一行，不要各自 if。**"""
    if capability not in driver.capabilities():
        raise CapabilityMissing(capability, getattr(driver, "name", "?"))

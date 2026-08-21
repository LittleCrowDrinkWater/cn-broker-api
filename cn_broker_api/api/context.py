"""路由要用的那几样依赖，装成一个对象传下去。

⭐ 为什么不让各路由自己去建：`cache`、`flight`、`queue` **必须各只有一个实例**。
各建一个的表现分别是「缓存永远是空的」「两个客户端进程」「两条并发的交易调用」，
而这三样都不报错。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cn_broker_api.config import Config
from cn_broker_api.serial_queue import AccountSerialQueue
from cn_broker_api.singleflight import SingleFlight
from cn_broker_api.state import HealthCache, LastRun


@dataclass(frozen=True)
class ApiContext:
    cfg: Config
    driver: Any
    cache: HealthCache
    last_run: LastRun
    flight: SingleFlight
    #: 交易调用的串行队列（按账户成批叫号）。**必须只有一个**：到客户端的连接是进程级单条。
    queue: AccountSerialQueue
    token: str
    repo_root: Path
    #: 看门狗。`None` ＝ 没装（诊断页要能区分"没装"和"装了没在跑"）。
    watchdog: Optional[Any] = None

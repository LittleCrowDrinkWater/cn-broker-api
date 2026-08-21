"""路由要用的那几样依赖，装成一个对象传下去。

⭐ 为什么不让各路由自己去建：`cache` 与 `flight` **必须是同一个实例**。
各建一个的表现分别是「缓存永远是空的」和「两个客户端进程」，而两者都不报错。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cn_broker_api.config import Config
from cn_broker_api.singleflight import SingleFlight
from cn_broker_api.state import HealthCache, LastRun


@dataclass(frozen=True)
class ApiContext:
    cfg: Config
    driver: Any
    cache: HealthCache
    last_run: LastRun
    flight: SingleFlight
    token: str
    repo_root: Path
    #: 看门狗。`None` ＝ 没装（诊断页要能区分"没装"和"装了没在跑"）。
    watchdog: Optional[Any] = None

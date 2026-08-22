"""看门狗的配置。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WatchdogConfig:
    """看门狗的配置。三条纪律（默认关 / 绝不填密码 / 只拉起绝不 kill）见 `watchdog` 模块。"""

    #: 默认关（见类 docstring 第一条）。
    enabled: bool = False
    #: 醒一次的间隔。默认 60 秒：进程枚举是零成本的，但拉起进程后客户端要几秒才到位，
    #: 太密的话会在同一次故障里连着拉好几次。
    interval_seconds: int = 60
    #: 工作时段（本地时间，闭区间）。默认覆盖开盘前到收盘后一点：
    #: 09:00 之前要给 09:00 那条登录任务把进程准备好，15:10 之后当天不再报单。
    window_start: str = "08:40"
    window_end: str = "15:10"
    #: 只在周一到周五工作。 刻意不判节假日：那要一份交易日历，而本服务不该有数据依赖。
    weekdays_only: bool = True
    #: 每个进程每天最多拉起几次。防的是"起来就自己退"的死循环——
    #: 那种情况下无限重启只会刷屏，而真正要做的是让人来看一眼。
    max_starts_per_day: int = 3

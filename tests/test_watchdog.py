"""看门狗的用例。

**每一条都对应一个具体的坏结果**，不是覆盖率：
拉起了不该拉的（时段外/周末/纸面驱动）｜没拉该拉的｜同时拉两个｜无限重启｜
跑去填密码｜什么都不留痕。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from cn_broker_api.config import WatchdogConfig
from cn_broker_api.drivers.capability import Capability
from cn_broker_api.singleflight import SingleFlight
from cn_broker_api.state import WatchdogState
from cn_broker_api.watchdog import Watchdog

#: 交易日盘中的一个时刻（周五 10:30）。
TRADING = datetime(2026, 8, 21, 10, 30)


class FakeDriver:
    """只回答「进程在不在」和「拉起来」的假驱动。⭐ 刻意**记下被拉起过什么**——
    「有没有拉」和「拉了几次」是这组用例唯一要断言的东西。"""

    name = "fake"

    def __init__(self, alive=None, caps=(Capability.DESKTOP_LOGIN,), boom=False):
        self.alive = dict(alive or {"Tdxw.exe": True, "TC.exe": True})
        self.caps = list(caps)
        self.started = []
        self.boom = boom

    def capabilities(self):
        return self.caps

    def desktop_processes(self):
        if self.boom:
            raise OSError("枚举进程炸了")
        return dict(self.alive)

    def start_desktop_process(self, name):
        self.started.append(name)
        self.alive[name] = True


def _dog(tmp_path: Path, driver, **over):
    cfg = WatchdogConfig(**{"enabled": True, "interval_seconds": 60,
                            "window_start": "08:40", "window_end": "15:10",
                            "weekdays_only": True, "max_starts_per_day": 3, **over})
    return Watchdog(driver, cfg,
                    state=WatchdogState(state_dir=tmp_path,
                                        max_starts_per_day=cfg.max_starts_per_day))


def test_all_processes_alive_means_no_action(tmp_path):
    d = FakeDriver()
    res = _dog(tmp_path, d).tick(now=TRADING)
    assert res["action"] == "none" and d.started == []


def test_a_missing_process_gets_started(tmp_path):
    d = FakeDriver({"Tdxw.exe": False, "TC.exe": True})
    res = _dog(tmp_path, d).tick(now=TRADING)
    assert res["action"] == "started" and d.started == ["Tdxw.exe"]


def test_only_one_process_per_tick(tmp_path):
    """🔴 两个都缺时**一次只拉一个**：客户端还在初始化就又弹一个窗口，
    而登录那侧的第一步是「先分类是哪道门」——两道门同时在场正是那一步最容易认错的场面。"""
    d = FakeDriver({"Tdxw.exe": False, "TC.exe": False})
    dog = _dog(tmp_path, d)
    dog.tick(now=TRADING)
    assert d.started == ["Tdxw.exe"], "配方的顺序就是拉起顺序，主程序先起"
    dog.tick(now=TRADING)
    assert d.started == ["Tdxw.exe", "TC.exe"]


def test_outside_the_window_it_does_nothing(tmp_path):
    """半夜三点把客户端拉起来，除了在桌面上弹个窗口没有任何用。"""
    d = FakeDriver({"Tdxw.exe": False})
    res = _dog(tmp_path, d).tick(now=datetime(2026, 8, 21, 3, 0))
    assert res["action"] == "none" and d.started == []
    assert "工作时段" in res["reason"]


def test_weekend_is_skipped(tmp_path):
    d = FakeDriver({"Tdxw.exe": False})
    res = _dog(tmp_path, d).tick(now=datetime(2026, 8, 22, 10, 30))   # 周六
    assert res["action"] == "none" and d.started == []


def test_disabled_means_it_never_acts(tmp_path):
    d = FakeDriver({"Tdxw.exe": False})
    res = _dog(tmp_path, d, enabled=False).tick(now=TRADING)
    assert res["action"] == "none" and d.started == []


def test_a_driver_without_desktop_login_is_left_alone(tmp_path):
    """纸面驱动没声明这个能力 ⇒ 看门狗不该去碰它，且要说出为什么（不能静默不动）。"""
    d = FakeDriver({"Tdxw.exe": False}, caps=())
    res = _dog(tmp_path, d).tick(now=TRADING)
    assert res["action"] == "none" and d.started == []
    assert "不管桌面进程" in res["reason"]


def test_daily_start_budget_stops_a_restart_loop(tmp_path):
    """🔴 「起来就自己退」时无限重启只会刷屏，真正该做的是让人来看一眼。"""
    d = FakeDriver({"Tdxw.exe": False})
    dog = _dog(tmp_path, d, max_starts_per_day=2)
    for _ in range(4):
        d.alive["Tdxw.exe"] = False        # 每次都又退了
        dog.tick(now=TRADING)
    assert d.started == ["Tdxw.exe", "Tdxw.exe"], "上限之外不许再拉"


def test_the_budget_survives_a_restart(tmp_path):
    """额度落盘：放内存的话「重启三次 ＝ 又拉了三次」。"""
    d = FakeDriver({"Tdxw.exe": False})
    _dog(tmp_path, d, max_starts_per_day=1).tick(now=TRADING)
    d.alive["Tdxw.exe"] = False
    fresh = _dog(tmp_path, d, max_starts_per_day=1)      # 新进程、同一个状态目录
    res = fresh.tick(now=TRADING)
    assert res.get("budget_used_up") and d.started == ["Tdxw.exe"]


def test_it_yields_when_another_action_holds_the_lock(tmp_path):
    """🔴 与 `/v1/session/ensure` 共用同一把单飞锁：不共用的表现是两个客户端进程。"""
    d = FakeDriver({"Tdxw.exe": False})
    flight = SingleFlight()
    flight.start()                                        # 假装 ensure 正在跑
    dog = _dog(tmp_path, d)
    dog.flight = flight
    res = dog.tick(now=TRADING)
    assert res["action"] == "none" and d.started == []
    assert "让开" in res["reason"]


def test_it_never_touches_the_password(tmp_path):
    """🔴 看门狗只管进程活着，**绝不填密码**：额度是每天一次，而它每分钟醒一次。

    判据落在"它压根没有登录这个动作"上——假驱动连 `ensure_logged_in` 都没实现，
    真去调就会 AttributeError。
    """
    d = FakeDriver({"Tdxw.exe": False})
    assert not hasattr(d, "ensure_logged_in")
    _dog(tmp_path, d).tick(now=TRADING)
    assert d.started == ["Tdxw.exe"]


def test_a_failing_enumeration_does_not_kill_the_watchdog(tmp_path):
    d = FakeDriver(boom=True)
    res = _dog(tmp_path, d).tick(now=TRADING)
    assert res["action"] == "none" and "枚举进程失败" in res["reason"]


def test_every_tick_leaves_a_trace(tmp_path):
    """一个不留痕的看门狗和一个没在跑的看门狗，在诊断页上不能长得一样。"""
    d = FakeDriver()
    dog = _dog(tmp_path, d)
    dog.tick(now=TRADING)
    got = dog.state.read()
    assert got["last_tick"]["reason"] == "进程齐了"
    assert got["last_tick"]["at"].startswith("2026-08-21T10:30")


def test_start_returns_false_when_disabled(tmp_path):
    assert _dog(tmp_path, FakeDriver(), enabled=False).start() is False


def test_start_returns_false_without_the_capability(tmp_path):
    assert _dog(tmp_path, FakeDriver(caps=())).start() is False

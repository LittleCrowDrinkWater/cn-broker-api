"""看门狗：盯着客户端进程在不在，掉了就把它拉起来。分工上它只保证**客户端**活着，
本服务自己活着由计划任务保证。

刻意不做的三件事，每一条都有代价：

① **绝不填密码**。密码额度按天算，而它每分钟醒一次——让它去登录，第一次失败就在没人
   看着的时候把当天额度烧光。⇒ 它保证「进程活着」，不是「通道可用」（中间还隔三道门）。
② **绝不 kill**。「没响应」和「没进程」是两件事，杀掉重启会打断正在飞的委托。
③ **一次只拉一个**。同时拉两个的话，客户端还在初始化就又弹一个窗口，
   而登录那侧第一步是「先分类是哪道门」——两道门同时在场正是最容易认错的场面。

⭐ 与 `/v1/session/ensure` 共用同一把单飞锁（不共用 ⇒ 两个客户端进程），
但**只在真要起进程时才抢**：枚举进程是零成本的，为它抢锁会让心跳把 ensure 挡在门外。
⚠️ 时段闸**刻意不判节假日**：引一份交易日历进来，等于给这个只该说 HTTP 的服务加一个
数据依赖，而多起一次客户端的代价只是桌面上多一个窗口。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, time as dtime
from typing import Any, Callable, Dict, Optional

from cn_broker_api.config import WatchdogConfig
from cn_broker_api.drivers.capability import Capability
from cn_broker_api.state import StartBudgetUsedUp, WatchdogState

logger = logging.getLogger(__name__)


def _parse_hhmm(s: str) -> dtime:
    hh, _, mm = str(s).partition(":")
    return dtime(int(hh), int(mm))


class Watchdog:
    """一个后台线程，每 `interval_seconds` 醒一次。

    `tick()` 是纯观测 + 最多一个动作，**返回它做了什么**——用例直接断言这个返回值，
    不需要真起线程、也不需要真有客户端。
    """

    def __init__(self, driver: Any, cfg: WatchdogConfig, *, state: WatchdogState,
                 flight: Any = None,
                 clock: Callable[[], datetime] = datetime.now) -> None:
        self.driver = driver
        self.cfg = cfg
        self.state = state
        self.flight = flight
        self.clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── 闸 ───────────────────────────────────────────────
    def _gate(self, now: datetime) -> Optional[str]:
        """返回不该动手的理由，`None` ＝ 可以动手。"""
        if not self.cfg.enabled:
            return "看门狗没开（watchdog.enabled = false）"
        if self.cfg.weekdays_only and now.weekday() >= 5:
            return f"周末（{now:%Y-%m-%d %A}）不看"
        start = _parse_hhmm(self.cfg.window_start)
        end = _parse_hhmm(self.cfg.window_end)
        if not (start <= now.time() <= end):
            return (f"不在工作时段 {self.cfg.window_start}~{self.cfg.window_end}"
                    f"（现在 {now:%H:%M}）")
        # 能力没声明就明确不干活。静默什么都不做，会让人以为看门狗在工作。
        if Capability.DESKTOP_LOGIN not in self.driver.capabilities():
            return f"驱动 {getattr(self.driver, 'name', '?')} 不管桌面进程，看门狗无事可做"
        return None

    # ── 一次心跳 ─────────────────────────────────────────
    def tick(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """醒一次。返回**这一次做了什么**，用例直接断言它。

        ⭐ 落痕收在这一处：五条分支各写一次的话，"少了一条痕"和"那条分支没走过"
        在页面上长得一样。闸挡住时刻意不落盘（时段外每分钟写一次文件换不来任何信息）。
        """
        now = now or self.clock()
        blocked = self._gate(now)
        if blocked:
            return {"action": "none", "reason": blocked}
        res = self._act(now)
        self.state.note_tick(res, now=now)
        return res

    def _act(self, now: datetime) -> Dict[str, Any]:
        """闸都过了之后真去看、真动手。**最多一个动作。**"""
        try:
            procs = self.driver.desktop_processes()
        except Exception as e:  # noqa: BLE001 — 看门狗自己不许把服务带崩
            logger.exception("[watchdog] 枚举进程失败")
            return {"action": "none",
                    "reason": f"枚举进程失败：{type(e).__name__}: {e}"}

        missing = [n for n, alive in procs.items() if not alive]
        if not missing:
            return {"action": "none", "reason": "进程齐了", "processes": procs}

        # 一次只拉一个（见模块 docstring 不做③）。配方的顺序就是拉起顺序。
        name = missing[0]
        try:
            nth = self.state.claim_start(name)
        except StartBudgetUsedUp as e:
            return {"action": "none", "reason": str(e), "processes": procs,
                    "budget_used_up": True}

        job_id = None
        mine = True
        if self.flight is not None:
            job_id, mine = self.flight.start()
            if not mine:
                # ⭐ 已经有一趟 ensure 在跑，它自己就会把进程拉起来 ⇒ 让开。
                #    额度已经记掉了（先记后起），少一次自动拉起比拉出两个客户端便宜。
                return {"action": "none",
                        "reason": f"有别的动作在跑（任务 {job_id}），让开",
                        "processes": procs}
        try:
            self.driver.start_desktop_process(name)
            res = {"action": "started", "process": name, "nth_today": nth,
                   "processes": procs,
                   "reason": f"{name} 不在跑 ⇒ 已拉起（今天第 {nth} 次）"}
        except Exception as e:  # noqa: BLE001 — 拉起失败要留痕，但不许把看门狗带崩
            logger.exception("[watchdog] 拉起 %s 失败", name)
            res = {"action": "failed", "process": name,
                   "reason": f"拉起 {name} 失败：{type(e).__name__}: {str(e)[:200]}"}
        finally:
            if self.flight is not None and job_id is not None and mine:
                self.flight.finish(job_id, {"ok": True, "watchdog": True})
        return res

    # ── 线程 ─────────────────────────────────────────────
    def _loop(self) -> None:
        logger.info("[watchdog] 起来了：每 %d 秒一次，时段 %s~%s%s",
                    self.cfg.interval_seconds, self.cfg.window_start,
                    self.cfg.window_end, "（只工作日）" if self.cfg.weekdays_only else "")
        while not self._stop.is_set():
            try:
                res = self.tick()
                if res.get("action") != "none":
                    logger.warning("[watchdog] %s", res.get("reason"))
            except Exception:  # noqa: BLE001 — 一次失败不该让看门狗整条死掉
                logger.exception("[watchdog] 这一次心跳出错，继续下一次")
            self._stop.wait(self.cfg.interval_seconds)
        logger.info("[watchdog] 退出")

    def start(self) -> bool:
        """起线程。**没开就不起**，返回是不是真起了。"""
        if not self.cfg.enabled:
            logger.info("[watchdog] 没开（watchdog.enabled = false），不起线程")
            return False
        if Capability.DESKTOP_LOGIN not in self.driver.capabilities():
            logger.warning("[watchdog] 驱动 %s 不管桌面进程 ⇒ 不起线程",
                           getattr(self.driver, "name", "?"))
            return False
        self._thread = threading.Thread(target=self._loop, name="watchdog", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

"""看门狗的落盘状态：今天各进程拉起过几次 + 上一次醒来做了什么。

与日闩同一个理由要落盘（重启不该把计数清零），但记的是**另一种额度**：
日闩管的是提交密码，这里管的是拉起进程。两种额度刻意分开计——
混成一个计数器，会让「客户端崩了三次」把当天的密码额度也吃掉。"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

from cn_broker_api.state.atomic import atomic_write_json
from cn_broker_api.state.start_budget_used_up import StartBudgetUsedUp

logger = logging.getLogger(__name__)


@dataclass
class WatchdogState:
    """今天各进程拉起过几次 + 上一次醒来做了什么。

     次数落盘：放内存的话「重启三次 ＝ 又拉了三次」，而它防的是"起来就自己退"的死循环。
     上次心跳也落盘：不留痕的看门狗和没在跑的看门狗，在诊断页上长得一模一样。
    """

    state_dir: Path
    max_starts_per_day: int = 3
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def _path(self) -> Path:
        return Path(self.state_dir) / "watchdog.json"

    def _read(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError) as e:
            # 同日闩：读不出来当成「已经用完」——保守那边的代价是人自己起一次客户端，
            # 乐观那边的代价是无限重启。
            logger.error("[watchdog] %s 读不出来（%s）⇒ 按次数已用完处置",
                         self._path, str(e)[:120])
            return {"__unreadable__": True}

    def _today(self, data: Dict[str, Any], *, day: Optional[date] = None) -> Dict[str, int]:
        d = (day or date.today()).isoformat()
        if data.get("day") != d:
            return {}                      # 跨天自动清零，不需要额外的清理逻辑
        return {str(k): int(v) for k, v in (data.get("starts") or {}).items()}

    def starts_today(self, *, day: Optional[date] = None) -> Dict[str, int]:
        return self._today(self._read(), day=day)

    def claim_start(self, name: str, *, day: Optional[date] = None) -> int:
        """要一次拉起额度，返回这是今天第几次。**先记后起**——同日闩：
        记完崩了会多记一次，而多记的代价（今天不再自动拉）比少记（无限重启）小得多。
        """
        d = (day or date.today()).isoformat()
        with self._lock:
            data = self._read()
            if data.get("__unreadable__"):
                raise StartBudgetUsedUp(
                    f"看门狗状态文件坏了（{self._path}）⇒ 今天不再自动拉起进程")
            counts = self._today(data, day=day)
            n = counts.get(name, 0)
            if n >= self.max_starts_per_day:
                raise StartBudgetUsedUp(
                    f"{name} 今天已经拉起过 {n} 次（上限 {self.max_starts_per_day}）⇒ 不再拉。"
                    f"起来就自己退多半是客户端那侧要人看一眼")
            counts[name] = n + 1
            data.update({"day": d, "starts": counts})
            atomic_write_json(self._path, data)
            logger.warning("[watchdog] 拉起 %s（今天第 %d 次，上限 %d）",
                           name, n + 1, self.max_starts_per_day)
            return n + 1

    def note_tick(self, payload: Dict[str, Any], *, now: Optional[datetime] = None) -> None:
        """记下这一次醒来看到了什么、做了什么。写不下去**不许**让看门狗本身失败。"""
        with self._lock:
            data = self._read()
            data.pop("__unreadable__", None)
            data["last_tick"] = {**payload,
                                 "at": (now or datetime.now()).isoformat(timespec="seconds")}
            try:
                atomic_write_json(self._path, data)
            except OSError as e:  # noqa: BLE001
                logger.warning("[watchdog] 状态写不下去：%s", str(e)[:120])

    def read(self) -> Dict[str, Any]:
        data = self._read()
        return {"starts_today": self.starts_today(),
                "max_starts_per_day": self.max_starts_per_day,
                "last_tick": data.get("last_tick")}

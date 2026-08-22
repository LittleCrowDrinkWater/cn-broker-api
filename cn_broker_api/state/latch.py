"""闩：还能不能再提交一次交易密码。**两个计数器，维度不同，刻意不合并**。

| | 数什么 | 什么时候清零 | 防的是 |
|---|---|---|---|
| 连续失败 | 连续没成功的提交 | **成功登录时**，不按天 | 券商锁定账户 |
| 每日提交 | 当天提交总数（含成功） | 每天午夜 | **我们自己的代码**循环提交 |

 2026-08-21 的教训是把两者混成了一个：那个闸数的是提交次数（成功也算）⇒ 设成 1 就是
「09:00 成功登了一次，11:00 客户端掉了就再登不回去」。而按天算本身也是错的——券商的
锁定计数只在成功登录时清零，密码真错了会一天一天地攒。防锁定的那个必须跨天累计。

 必须落盘：放内存的话「重启三次 ＝ 无声无息地试了三次密码」。
 **先记后点、且先按失败记**：多记的代价是要人工登一次，少记的代价是悄悄多试一次密码。
 只有**真提交**才计数——认不准、星号宽度核不上那些分支本来就没提交。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date
from pathlib import Path
from typing import Dict, Optional

from cn_broker_api.state.atomic import atomic_write_json
from cn_broker_api.state.submit_blocked import SubmitBlocked

logger = logging.getLogger(__name__)

#: 连续失败计数放这儿。**单独一个文件、不带日期**——它的生命周期是「直到成功登录」，
#: 与按天清零的那个不一样。混进同一个文件会让"跨天"这件事变得含糊。
FAILURES_FILE = "latch-failures.json"


class SubmitLatch:
    """还能不能再提交一次密码。两个计数器都**落盘**，见模块 docstring。"""

    def __init__(self, state_dir: Path, max_per_day: int = 10,
                 max_consecutive_failures: int = 3) -> None:
        self.state_dir = Path(state_dir)
        self.max_per_day = max(0, int(max_per_day))
        self.max_consecutive_failures = max(0, int(max_consecutive_failures))
        self._lock = threading.Lock()

    # ── 落盘读写 ─────────────────────────────────────────
    def _path(self, day: date) -> Path:
        return self.state_dir / f"latch-{day.isoformat()}.json"

    @property
    def _fail_path(self) -> Path:
        return self.state_dir / FAILURES_FILE

    @staticmethod
    def _read_counts(p: Path, unreadable_value: int) -> Dict[str, int]:
        """读一个 {账户: 次数} 文件。

         读不出来当成「已经用完」，不是「还没用过」。坏文件的两种解释里，
        保守那一边的代价是要人工登一次；乐观那一边的代价是多试密码。
        """
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.error("[latch] %s 读不出来（%s）⇒ 按已用完处置", p, str(e)[:120])
            return {"__unreadable__": unreadable_value}
        return {str(k): int(v) for k, v in (data or {}).items()}

    # ── 查（诊断页要用）─────────────────────────────────
    def used(self, account: str, *, day: Optional[date] = None) -> int:
        return self._read_counts(self._path(day or date.today()),
                                 self.max_per_day + 1).get(account or "", 0)

    def remaining(self, account: str, *, day: Optional[date] = None) -> int:
        counts = self._read_counts(self._path(day or date.today()),
                                   self.max_per_day + 1)
        if "__unreadable__" in counts:
            return 0
        return max(0, self.max_per_day - counts.get(account or "", 0))

    def consecutive_failures(self, account: str) -> int:
        return self._read_counts(self._fail_path,
                                 self.max_consecutive_failures + 1).get(account or "", 0)

    # ── 改 ───────────────────────────────────────────────
    def claim(self, account: str, *, day: Optional[date] = None) -> None:
        """要一次提交额度。**先记后点**：本函数返回之后调用方才可以去点【登录】。

        两个闸都要过。用完了抛 `SubmitBlocked` —— 接口上刻意**不提供 force**：
        能不能再试一次由持有凭据的这个进程独家裁决，调用方连表达「再试一次」的词都不该有。

         连续失败**先按失败记**（乐观的做法是等结果，但那样崩在中间就少记一次）。
        成功了由 `settle(ok=True)` 清零。
        """
        d = day or date.today()
        acc = account or ""
        with self._lock:
            fails = self._read_counts(self._fail_path, self.max_consecutive_failures + 1)
            if "__unreadable__" in fails:
                raise SubmitBlocked(
                    f"连续失败计数文件坏了（{self._fail_path}）⇒ 不再自动提交密码，"
                    f"请人工登录一次（成功登录会把它清零）")
            nf = fails.get(acc, 0)
            if nf >= self.max_consecutive_failures:
                raise SubmitBlocked(
                    f"账户 {acc or '(默认)'} 已连续 {nf} 次提交密码没成功"
                    f"（上限 {self.max_consecutive_failures}）⇒ 不再自动提交。"
                    f"多半是库里那个密码不对：请人工登录一次确认，"
                    f"**成功登录会把这个计数清零**。"
                    f"（券商的锁定策略未核实，所以这里不拿次数去试）")

            counts = self._read_counts(self._path(d), self.max_per_day + 1)
            if "__unreadable__" in counts:
                raise SubmitBlocked(
                    f"日闩文件坏了（{self._path(d)}）⇒ 今天不再自动提交密码，请人工登录一次")
            nd = counts.get(acc, 0)
            if nd >= self.max_per_day:
                raise SubmitBlocked(
                    f"账户 {acc or '(默认)'} 今天已提交过 {nd} 次交易密码"
                    f"（上限 {self.max_per_day}）⇒ 不再自动提交。"
                    f"这个闸防的是我们自己的代码循环提交——连着提交这么多次本身就说明有问题，"
                    f"该去看日志而不是调大它")

            counts[acc] = nd + 1
            atomic_write_json(self._path(d), counts)
            fails[acc] = nf + 1                      # 先按失败记，成功再清零
            atomic_write_json(self._fail_path, fails)
            logger.warning("[latch] 账户 %s 提交密码：今天第 %d 次（上限 %d）；"
                           "连续未成功 %d 次（上限 %d）",
                           acc or "(默认)", nd + 1, self.max_per_day,
                           nf + 1, self.max_consecutive_failures)

    def settle(self, account: str, ok: bool) -> None:
        """把这一趟的结果落下来。**成功就把连续失败清零**。

         失败**什么都不做**：`claim()` 已经先按失败记过了。在这里再加一次就是记两遍。
         调用方在拿到结果之后必须调它——不调的表现是「登录成功了，但连续失败计数一直涨」，
        几天之后闸就把自己关死了。
        """
        if not ok:
            return
        acc = account or ""
        with self._lock:
            fails = self._read_counts(self._fail_path, self.max_consecutive_failures + 1)
            fails.pop("__unreadable__", None)
            if fails.get(acc):
                logger.info("[latch] 账户 %s 登录成功 ⇒ 连续失败计数 %d 清零",
                            acc or "(默认)", fails[acc])
            fails[acc] = 0
            atomic_write_json(self._fail_path, fails)

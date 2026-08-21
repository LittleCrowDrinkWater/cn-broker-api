"""机器写的状态：日闩、上次登录结果、健康检查缓存。

## 与配置刻意反向

配置是人写的、TOML、只读；状态是程序写的、JSON、人别去改。混在一处的话，程序写一次状态就把
人手写的注释全冲掉了。

## 日闩为什么必须落盘

它记的是「本账户今天提交过几次交易密码」。放内存的话重启服务就清零，
于是「**重启三次＝无声无息地试了三次密码**」——而券商在密码连续输错之后会怎么做，
各家不同且本项目未核实。

⭐ 计数点在**点【登录】之前**（先记后点）。中间崩了会多记一次，而多记是安全方向：
多记的代价是今天不再自动登录（人工登一次即可），少记的代价是无声无息地多试一次密码。

⭐ 只有**真提交**才计数。认不准、星号宽度核不上那些分支是「已清空、未提交」，
本来就不算一次错误尝试，不能计入。

## 密码为什么反过来——只在内存

`cred_source = "request"` 时密码随调用下发，只在进程内存里按账户留当天一份。
两者刻意反向：**日闩怕丢掉，密码怕留下来。**
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SubmitBlocked(Exception):
    """今天这个账户的密码提交次数已经用完。**不是错误，是闸门生效了**——
    调用方该把它翻译成一句人话让人自己去登，而不是当异常上报。"""


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """先写临时文件再改名。**日闩不能写坏**：写一半断电留下个坏 JSON，下次读不出来就等于
    计数被清零，而清零正是这个文件要防的那件事。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class SubmitLatch:
    """按 (账户, 日期) 记密码提交次数。**落盘**，见模块 docstring。"""

    def __init__(self, state_dir: Path, max_per_day: int = 1) -> None:
        self.state_dir = Path(state_dir)
        self.max_per_day = max(0, int(max_per_day))
        self._lock = threading.Lock()

    def _path(self, day: date) -> Path:
        return self.state_dir / f"latch-{day.isoformat()}.json"

    def _read(self, day: date) -> Dict[str, int]:
        p = self._path(day)
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            # 🔴 读不出来当成「已经用完」，不是「还没用过」。坏文件的两种解释里，
            #    保守那一边的代价是今天要人工登一次；乐观那一边的代价是多试密码。
            logger.error("[latch] %s 读不出来（%s）⇒ 按已用完处置", p, str(e)[:120])
            return {"__unreadable__": self.max_per_day + 1}
        return {str(k): int(v) for k, v in (data or {}).items()}

    def used(self, account: str, *, day: Optional[date] = None) -> int:
        return self._read(day or date.today()).get(account or "", 0)

    def remaining(self, account: str, *, day: Optional[date] = None) -> int:
        d = day or date.today()
        counts = self._read(d)
        if "__unreadable__" in counts:
            return 0
        return max(0, self.max_per_day - counts.get(account or "", 0))

    def claim(self, account: str, *, day: Optional[date] = None) -> None:
        """要一次提交额度。**先记后点**：本函数返回之后调用方才可以去点【登录】。

        用完了抛 `SubmitBlocked` —— 接口上刻意**不提供 force**：能不能再试一次由持有凭据的
        这个进程独家裁决，调用方连表达「再试一次」的词都不该有。
        """
        d = day or date.today()
        acc = account or ""
        with self._lock:
            counts = self._read(d)
            if "__unreadable__" in counts:
                raise SubmitBlocked(
                    f"日闩文件坏了（{self._path(d)}）⇒ 今天不再自动提交密码，请人工登录一次")
            n = counts.get(acc, 0)
            if n >= self.max_per_day:
                raise SubmitBlocked(
                    f"账户 {acc or '(默认)'} 今天已提交过 {n} 次交易密码"
                    f"（上限 {self.max_per_day}）⇒ 不再自动提交。"
                    f"券商的锁定策略未核实，不拿尝试次数去试；请人工登录一次")
            counts[acc] = n + 1
            _atomic_write_json(self._path(d), counts)
            logger.warning("[latch] 账户 %s 第 %d 次提交密码（上限 %d）",
                           acc or "(默认)", n + 1, self.max_per_day)


@dataclass
class CachedHealth:
    """健康检查的缓存条目。**必须带产出时刻**——诊断页要显示"数据于 N 秒前"，
    静默展示旧数据的状态页比没有更糟（人会照着几分钟前的画面做判断）。"""

    payload: Dict[str, Any]
    at: datetime

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        return max(0.0, ((now or datetime.now()) - self.at).total_seconds())


class HealthCache:
    """健康检查结果的内存缓存。

    🔴 **便宜是硬要求**：「交易账号登录了没」那一项要连客户端、查一次资产，几秒钟且占用
    账户串行槽。诊断页每 5 秒轮询一次的话，不缓存等于整天骚扰交易通道
    ⇒ `GET /v1/health` 读缓存，只有 `POST /v1/health/refresh` 才真去探。
    """

    def __init__(self, ttl_seconds: int = 30) -> None:
        self.ttl = max(0, int(ttl_seconds))
        self._lock = threading.Lock()
        self._entry: Optional[CachedHealth] = None

    def get(self) -> Optional[CachedHealth]:
        with self._lock:
            return self._entry

    def fresh(self, *, now: Optional[datetime] = None) -> Optional[CachedHealth]:
        e = self.get()
        if e is not None and e.age_seconds(now) <= self.ttl:
            return e
        return None

    def put(self, payload: Dict[str, Any], *, now: Optional[datetime] = None) -> CachedHealth:
        e = CachedHealth(payload=payload, at=now or datetime.now())
        with self._lock:
            self._entry = e
        return e


@dataclass
class PasswordVault:
    """`cred_source = "request"` 时的密码存放处：**只在内存，绝不落盘**。

    ⭐ 按 (账户, 日期) 存：跨天自动失效，不需要额外的清理逻辑，也不会让昨天下发的密码
    在今天被悄悄复用。
    """

    _slots: Dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def put(self, account: str, password: str, *, day: Optional[date] = None) -> None:
        with self._lock:
            self._slots[account or ""] = ((day or date.today()).isoformat(), password)

    def get(self, account: str, *, day: Optional[date] = None) -> Optional[str]:
        d = (day or date.today()).isoformat()
        with self._lock:
            got = self._slots.get(account or "")
        if not got or got[0] != d:
            return None
        return got[1]

    def clear(self) -> None:
        with self._lock:
            self._slots.clear()

    def accounts_with_password(self, *, day: Optional[date] = None) -> list:
        """哪些账户手上有密码。**只报账户名，绝不报密码**——诊断页要显示这个。"""
        d = (day or date.today()).isoformat()
        with self._lock:
            return sorted(a for a, (day_s, _) in self._slots.items() if day_s == d)


@dataclass
class LastRun:
    """上一次 `session/ensure` 的结果，落盘一份给诊断页看（不含任何凭据）。"""

    state_dir: Path

    @property
    def _path(self) -> Path:
        return Path(self.state_dir) / "last-ensure.json"

    def write(self, ok: bool, detail: str, *, now: Optional[datetime] = None) -> None:
        try:
            _atomic_write_json(self._path, {
                "ok": bool(ok), "detail": str(detail)[:800],
                "at": (now or datetime.now()).isoformat(timespec="seconds")})
        except OSError as e:  # noqa: BLE001 — 记不下来不该让登录本身失败
            logger.warning("[state] 上次登录结果写不下去：%s", str(e)[:120])

    def read(self) -> Optional[Dict[str, Any]]:
        if not self._path.exists():
            return None
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

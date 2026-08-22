"""无控制台那条路：`pythonw.exe` 下 `sys.stdout` / `sys.stderr` 是 `None`。

计划任务跑的就是 `pythonw`。裸调 `sys.stdout.reconfigure(...)` 在那里抛 AttributeError，
而 stderr 也是 None ⇒ traceback 无处可写 ⇒ **服务静默退出 1、日志里一个字都没有**。
2026-08-22 装计划任务时真踩到了；在那之前这个服务只在有控制台的地方跑过。
"""
from __future__ import annotations

import re
from pathlib import Path

from cn_broker_api.stdio import init_stdio

PKG = Path(__file__).resolve().parents[1] / "cn_broker_api"

#: 裸调 `sys.stdout.reconfigure(...)` / `sys.stderr.reconfigure(...)`。
BARE = re.compile(r"^\s*sys\.std(out|err)\.reconfigure\(", re.M)


def test_it_survives_streams_that_are_none(monkeypatch):
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr("sys.stderr", None)
    init_stdio()                        # 不抛就是对的


def test_nobody_reconfigures_stdio_bare(monkeypatch):
    """🔴 归一 stdio 只许走 `init_stdio()`。

    判据落在**源码**上而不是行为上：真正致命的那一处在 `login.py` 的**模块顶层**，
    import 期就炸——任何靠调用函数来验的用例都够不着它。
    """
    offenders = [str(p.relative_to(PKG)) for p in PKG.rglob("*.py")
                 if p.name != "stdio.py" and BARE.search(p.read_text(encoding="utf-8"))]
    assert not offenders, f"这些地方要改成 init_stdio()：{offenders}"

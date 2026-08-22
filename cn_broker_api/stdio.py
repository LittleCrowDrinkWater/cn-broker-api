"""把标准输出与标准错误归一到 utf-8。**每个入口都要在做任何输出之前调一次。**

本机控制台是 GBK，中文（更不用说符号）会在最不该崩的时候崩——实测是登录流程正走到一半。
`logging` 默认写 stderr，所以两条都要归，只归 stdout 等于没归。
"""
from __future__ import annotations

import sys


def init_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass          # 被重定向到不支持 reconfigure 的对象时不该因此起不来

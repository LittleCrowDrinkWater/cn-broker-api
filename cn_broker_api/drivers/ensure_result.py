"""「把状态弄到可用」的结果。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnsureResult:
    """「把状态弄到可用」的结果。`acted` 表示这一趟**真动手过**（过门 / 填过密码）。

     `acted` 不是流水账，它决定要不要收窗口：本来就登着的时候不动窗口——那多半是人正
    看着它。"弄完自动最小化"说的是无人值守那一趟。
    """

    ok: bool
    detail: str
    acted: bool = False

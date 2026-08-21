"""落盘的公共动作：原子写 JSON。

单独一个模块而不是塞在某个类边上：三处状态（日闩、看门狗、上次登录结果）都要它，
而"写坏了"对这三处的后果各不相同、都很贵。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
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

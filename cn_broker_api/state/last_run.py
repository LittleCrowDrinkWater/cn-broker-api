"""上一次 `session/ensure` 的结果，落盘一份给诊断页看（不含任何凭据）。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from cn_broker_api.state.atomic import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass
class LastRun:
    """上一次 `session/ensure` 的结果，落盘一份给诊断页看（不含任何凭据）。"""

    state_dir: Path

    @property
    def _path(self) -> Path:
        return Path(self.state_dir) / "last-ensure.json"

    def write(self, ok: bool, detail: str, *, now: Optional[datetime] = None) -> None:
        try:
            atomic_write_json(self._path, {
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

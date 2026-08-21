"""健康检查的一条缓存记录。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class CachedHealth:
    """健康检查的缓存条目。**必须带产出时刻**——诊断页要显示"数据于 N 秒前"，
    静默展示旧数据的状态页比没有更糟（人会照着几分钟前的画面做判断）。"""

    payload: Dict[str, Any]
    at: datetime

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        return max(0.0, ((now or datetime.now()) - self.at).total_seconds())

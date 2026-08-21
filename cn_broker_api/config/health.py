"""自检的配置：缓存多久、探针用哪只票。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HealthConfig:
    #: `GET /v1/health` 返回缓存 + 年龄；只有 `POST /v1/health/refresh` 才真去探。
    #: 🔴 便宜是硬要求：第②项要连客户端、占串行槽，而诊断页每 5 秒轮询一次。
    cache_seconds: int = 30
    #: 行情探针用的票：**要挑最活跃的**。冷门票在集合竞价刚开始时盘口可能全空，
    #: 那会把"客户端行情没问题"读成故障——探针的假警报比不探更糟。
    probe_symbol: str = "000001.SZ"

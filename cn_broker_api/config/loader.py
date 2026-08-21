"""读配置。**缺文件不是错**——用全套默认值起得来，只是驱动会因为
`tdx_home` 没给而在第一次真调用时明确失败（而不是 import 期就把服务拖挂）。"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional

from cn_broker_api.config.error import ConfigError
from cn_broker_api.config.health import HealthConfig
from cn_broker_api.config.paths import DEFAULT_CONFIG_PATH, candidates
from cn_broker_api.config.root import Config
from cn_broker_api.config.server import ServerConfig
from cn_broker_api.config.tdxquant import TdxQuantConfig
from cn_broker_api.config.watchdog import WatchdogConfig


def _as_path(v: Any) -> Optional[Path]:
    if v is None or v == "":
        return None
    return Path(str(v)).expanduser()


def _hhmm(v: Any, default: str, where: str) -> str:
    """校 `"HH:MM"`。**写歪了要当场报错，不要静默用默认值**：一个把工作时段写成
    `"9:00 "` 的配置，静默退回默认值的表现是"看门狗在我没让它工作的时段动手了"，
    而那时候人已经不记得自己写过什么。
    """
    if v is None or v == "":
        return default
    s = str(v).strip()
    try:
        hh, _, mm = s.partition(":")
        h, m = int(hh), int(mm)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{where} 要写成 HH:MM，收到 {v!r}") from e
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ConfigError(f"{where} 超出范围：{v!r}")
    return f"{h:02d}:{m:02d}"


def _dotted_keys(raw: Dict[str, Any], prefix: str = "") -> list:
    """把嵌套的 TOML 摊成点号键，好和 `describe()` 的行对上。"""
    out = []
    for k, v in (raw or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.extend(_dotted_keys(v, key + "."))
        else:
            out.append(key)
    return out


def _first_existing() -> Path:
    """候选位置里第一个真存在的；都不存在就返回默认那个（让报错文案指向它）。

    ⭐ 返回默认那个而不是 None：「配置在哪」这句话在报错里必须是具体路径，
    不然人不知道该把文件放哪儿。
    """
    for c in candidates():
        if c.exists():
            return c
    return DEFAULT_CONFIG_PATH


def load(path: Optional[Path] = None) -> Config:
    """读配置。**缺文件不是错**——用全套默认值起得来，只是驱动会因为 `tdx_home`
    没给而在第一次真调用时明确失败（而不是 import 期就把服务拖挂）。"""
    p = path or _as_path(os.environ.get("CN_BROKER_API_CONFIG")) or _first_existing()
    raw: Dict[str, Any] = {}
    if p.exists():
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"配置读不出来（{p}）：{e}") from e

    srv = raw.get("server") or {}
    hea = raw.get("health") or {}
    drv = raw.get("driver") or {}
    tq = (drv.get("tdxquant") if isinstance(drv, dict) else None) or {}

    state_dir = _as_path(srv.get("state_dir")) or (p.parent / "cn-broker-api-state")
    cred_source = str(tq.get("cred_source") or "file").strip().lower()
    if cred_source not in ("file", "request"):
        raise ConfigError(f"cred_source 只能是 file / request，收到 {cred_source!r}")

    transport = str(tq.get("transport") or "mcp").strip().lower()
    if transport not in ("mcp", "ctypes"):
        raise ConfigError(f"transport 只能是 mcp / ctypes，收到 {transport!r}")

    name = str((drv.get("name") if isinstance(drv, dict) else None) or "tdxquant").lower()
    if name not in ("tdxquant", "paper"):
        raise ConfigError(f"driver.name 只能是 tdxquant / paper，收到 {name!r}")

    wd = raw.get("watchdog") or {}
    watchdog = WatchdogConfig(
        enabled=bool(wd.get("enabled", False)),
        interval_seconds=max(10, int(wd.get("interval_seconds") or 60)),
        window_start=_hhmm(wd.get("window_start"), "08:40", "watchdog.window_start"),
        window_end=_hhmm(wd.get("window_end"), "15:10", "watchdog.window_end"),
        weekdays_only=bool(wd.get("weekdays_only", True)),
        max_starts_per_day=max(0, int(wd.get("max_starts_per_day") or 3)))

    return Config(
        server=ServerConfig(port=int(srv.get("port") or 17710),
                            token_file=_as_path(srv.get("token_file")),
                            state_dir=state_dir),
        health=HealthConfig(cache_seconds=int(hea.get("cache_seconds") or 30),
                            probe_symbol=str(hea.get("probe_symbol") or "000001.SZ")),
        tdxquant=TdxQuantConfig(
            tdx_home=_as_path(tq.get("tdx_home")),
            mcp_url=str(tq.get("mcp_url") or "http://127.0.0.1:17709").rstrip("/"),
            cred_source=cred_source,
            cred_file=_as_path(tq.get("cred_file")),
            max_password_submits_per_day=int(tq.get("max_password_submits_per_day") or 10),
            max_consecutive_failures=int(tq.get("max_consecutive_failures") or 3),
            transport=transport,
            cancel_confirm_timeout=float(tq.get("cancel_confirm_timeout") or 5.0),
            cancel_confirm_interval=float(tq.get("cancel_confirm_interval") or 1.0)),
        watchdog=watchdog,
        driver=name,
        source_path=p if p.exists() else None,
        provided=tuple(_dotted_keys(raw)),
    )

"""全部配置的汇总，以及「有哪些可配置」这份清单。

`describe()` 是从生效的那个对象上**现算**出来的，所以它永远和代码同步——
写在文档里的那种清单会慢慢和代码分叉。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from cn_broker_api.config.health import HealthConfig
from cn_broker_api.config.server import ServerConfig
from cn_broker_api.config.tdxquant import TdxQuantConfig
from cn_broker_api.config.watchdog import WatchdogConfig


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    health: HealthConfig
    tdxquant: TdxQuantConfig
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    #: 选哪个驱动。`paper` 不连任何东西——联调、CI、以及「出问题时分清是契约错还是
    #: 驱动错」都靠它。
    driver: str = "tdxquant"
    source_path: Optional[Path] = None
    #: 配置文件里**真的写了**哪些键（点号路径）。用来在启动日志和诊断页上把
    #: 「你写的」和「在吃默认值」分开。
    #: 这不是装饰：漏写一项和写了一项写错值，现象完全不同，而只看最终值分不出来。
    provided: Tuple[str, ...] = ()

    @property
    def token_file(self) -> Path:
        return self.server.token_file or (self.server.state_dir / "token.txt")

    def describe(self) -> List[Tuple[str, str, bool]]:
        """全部配置项的 (点号键, 生效值, 是不是文件里写的)。

        启动时打印它、诊断页显示它。**「有哪些可配置」不该只存在于文档里**——
        文档会和代码分叉，而这份清单是从生效的那个对象上现算出来的。
        """
        rows = [
            ("server.port", str(self.server.port)),
            ("server.state_dir", str(self.server.state_dir)),
            ("server.token_file", str(self.token_file)),
            ("health.cache_seconds", str(self.health.cache_seconds)),
            ("health.probe_symbol", self.health.probe_symbol),
            ("driver.name", self.driver),
            ("driver.tdxquant.tdx_home", str(self.tdxquant.tdx_home or "(未设)")),
            ("driver.tdxquant.mcp_url", self.tdxquant.mcp_url),
            ("driver.tdxquant.transport", self.tdxquant.transport),
            ("driver.tdxquant.desktop_mode", self.tdxquant.desktop_mode),
            ("driver.tdxquant.cred_source", self.tdxquant.cred_source),
            ("driver.tdxquant.cred_file", str(self.tdxquant.cred_file or "(未设)")),
            ("driver.tdxquant.max_password_submits_per_day",
             str(self.tdxquant.max_password_submits_per_day)),
            ("driver.tdxquant.max_consecutive_failures",
             str(self.tdxquant.max_consecutive_failures)),
            ("driver.tdxquant.cancel_confirm_timeout",
             str(self.tdxquant.cancel_confirm_timeout)),
            ("driver.tdxquant.cancel_confirm_interval",
             str(self.tdxquant.cancel_confirm_interval)),
            ("watchdog.enabled", "true" if self.watchdog.enabled else "false"),
            ("watchdog.interval_seconds", str(self.watchdog.interval_seconds)),
            ("watchdog.window_start", self.watchdog.window_start),
            ("watchdog.window_end", self.watchdog.window_end),
            ("watchdog.weekdays_only", "true" if self.watchdog.weekdays_only else "false"),
            ("watchdog.max_starts_per_day", str(self.watchdog.max_starts_per_day)),
        ]
        given = set(self.provided)
        return [(k, v, k in given) for k, v in rows]

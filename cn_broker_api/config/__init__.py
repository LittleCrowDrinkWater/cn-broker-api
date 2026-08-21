"""配置：人写的 TOML，装的全是「只属于这台机器」的值（客户端装在哪、资金账号、凭据路径）。

- 位置见 `paths`；仓库里只有 `config.example.toml` 那份模板。
- 选 TOML 是因为**能写注释**，而 `tomllib` **只能读不能写**——本服务物理上改不了你的配置。
- 与状态（程序写的 JSON，在 `state_dir`）刻意分开：混在一处，程序写一次状态就把人写的
  注释全冲掉了。
- 一个文件一段配置；`root` 是汇总加「有哪些可配置」那份清单，`loader` 只管读。
"""
from cn_broker_api.config.contract import CONTRACT_VERSION
from cn_broker_api.config.error import ConfigError
from cn_broker_api.config.health import HealthConfig
from cn_broker_api.config.loader import load
from cn_broker_api.config.paths import DEFAULT_CONFIG_PATH
from cn_broker_api.config.root import Config
from cn_broker_api.config.server import ServerConfig
from cn_broker_api.config.tdxquant import TdxQuantConfig
from cn_broker_api.config.watchdog import WatchdogConfig

__all__ = ["CONTRACT_VERSION", "Config", "ConfigError", "DEFAULT_CONFIG_PATH",
           "HealthConfig", "ServerConfig", "TdxQuantConfig", "WatchdogConfig", "load"]

"""配置：人写的 TOML，全在仓库之外。

## 为什么配置不在仓库里

本仓库计划公开，而配置里装的全是「这台机器特有」的东西：客户端装在哪、资金账号、
凭据文件路径。放进仓库 = 每次 `git pull` 都要处理冲突，且总有一天会把账号推上去。

⇒ 配置文件路径由环境变量 `CN_BROKER_API_CONFIG` 给，缺省 `../cn-broker-api.toml`
（仓库的**上一级**，刚好在 git 视野之外）。仓库里只有 `config.example.toml`。

## 为什么是 TOML 而不是 JSON

**能写注释。** 「配置项要注明用途和默认值的由来，不要只留一个裸值」这条规矩在 JSON 里
没法落地。而 `tomllib` 是标准库且**只能读不能写**——这个"缺点"正好把纪律焊死：
本服务物理上改不了你的配置，手写的注释永远不会被程序冲掉。

## 配置（人写）与状态（机器写）刻意分开

| | 配置 | 状态 |
|---|---|---|
| 谁写 | 只有人 | 只有程序 |
| 格式 | TOML | JSON |
| 位置 | `CN_BROKER_API_CONFIG` | `state_dir` |
| 手改 | 随时 | **绝不** |

两类东西混在一个文件里，程序写一次状态就把人写的注释全冲掉了。

## 这个包怎么分的

一个文件一个类：`server` / `health` / `tdxquant` / `watchdog` 各是一段配置，
`root` 是它们的汇总加上「有哪些可配置」那份清单，`loader` 只管把 TOML 读成对象。
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

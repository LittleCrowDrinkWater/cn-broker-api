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
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

#: 配置文件默认在仓库上一级——**刻意不在仓库里**（见模块 docstring）。
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "cn-broker-api.toml"

#: 契约版本。**后端启动时校这一个整数，不匹配直接拒跑、不做兼容适配。**
#: 进程内的 import 在启动就炸，HTTP 契约会在凌晨三点静默不对 ⇒ 必须显式对版本。
CONTRACT_VERSION = 1


class ConfigError(Exception):
    """配置缺失或不合法。**可捕获的异常而不是 SystemExit**：入口要翻译成一句人话 +
    非零退出码，测试要能断言它，两种处置不该由本模块决定。"""


@dataclass(frozen=True)
class ServerConfig:
    #: 挨着客户端的 17709，好记。**只绑 127.0.0.1 且不可配**——这个端口能下单，
    #: 让它监听外网是一个不该由配置项提供的选项。
    port: int = 17710
    #: token 文件。缺省放 state_dir 下；首次启动自动生成。
    token_file: Optional[Path] = None
    #: 机器写的状态放这儿（日闩、上次登录结果、health 缓存）。
    state_dir: Path = field(default_factory=lambda: DEFAULT_CONFIG_PATH.parent
                            / "cn-broker-api-state")


@dataclass(frozen=True)
class HealthConfig:
    #: `GET /v1/health` 返回缓存 + 年龄；只有 `POST /v1/health/refresh` 才真去探。
    #: 🔴 便宜是硬要求：第②项要连客户端、查一次资产、抢账户串行槽，几秒钟一次。
    #: 状态页每 5 秒轮询一次的话，不缓存等于整天骚扰交易通道。
    cache_seconds: int = 30
    #: 行情探针用的票：**要挑最活跃的**。冷门票在集合竞价刚开始时盘口可能全空，
    #: 那会把"客户端行情没问题"读成故障——探针的假警报比不探更糟。
    probe_symbol: str = "000001.SZ"


@dataclass(frozen=True)
class TdxQuantConfig:
    #: 客户端安装根目录。**本服务里唯一写死机器路径的地方**：补丁页面
    #: （`webs/cfg/aireq.html`）、可执行文件都从这里推。两处各写一份的话，换客户端时
    #: 改一处、另一处静默指向不存在的目录——而"文件不在"会被自检读成"补丁没打"。
    tdx_home: Optional[Path] = None
    #: MCP over HTTP 的地址。端口属主是客户端进程自己（`Tdxw.exe`）。
    mcp_url: str = "http://127.0.0.1:17709"
    #: 交易密码从哪来：
    #:   file    ＝ 本服务自己读 `cred_file`（搬家阶段，行为与搬家前逐字节相同）
    #:   request ＝ 后端在调用里带过来，本服务**只在内存留当天一份、绝不落盘**
    #: 见设计稿 §5.6：凭据迁移自带 DB 迁移 + 后端界面 + 真机验证，不混进搬家。
    cred_source: str = "file"
    #: `cred_source = "file"` 时的凭据文件。**必须在仓库之外。**
    cred_file: Optional[Path] = None
    #: 每个账户每天最多提交几次交易密码。
    #: ⚠️ 券商的锁定策略**未核实**（错几次会怎样、锁多久、要不要人工解锁都不知道）
    #: ⇒ 不拿尝试次数去试。想调大先去券商确认策略，别按感觉调。
    max_password_submits_per_day: int = 1


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    health: HealthConfig
    tdxquant: TdxQuantConfig
    #: 选哪个驱动。`paper` 不连任何东西——联调、CI、以及「出问题时分清是契约错还是
    #: 驱动错」都靠它。
    driver: str = "tdxquant"
    source_path: Optional[Path] = None

    @property
    def token_file(self) -> Path:
        return self.server.token_file or (self.server.state_dir / "token.txt")


def _as_path(v: Any) -> Optional[Path]:
    if v is None or v == "":
        return None
    return Path(str(v)).expanduser()


def load(path: Optional[Path] = None) -> Config:
    """读配置。**缺文件不是错**——用全套默认值起得来，只是驱动会因为 `tdx_home`
    没给而在第一次真调用时明确失败（而不是 import 期就把服务拖挂）。"""
    p = path or _as_path(os.environ.get("CN_BROKER_API_CONFIG")) or DEFAULT_CONFIG_PATH
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

    name = str((drv.get("name") if isinstance(drv, dict) else None) or "tdxquant").lower()
    if name not in ("tdxquant", "paper"):
        raise ConfigError(f"driver.name 只能是 tdxquant / paper，收到 {name!r}")

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
            max_password_submits_per_day=int(tq.get("max_password_submits_per_day") or 1)),
        driver=name,
        source_path=p if p.exists() else None,
    )

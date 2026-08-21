"""服务自身的配置：端口、token、状态目录。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cn_broker_api.config.paths import DEFAULT_CONFIG_PATH


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

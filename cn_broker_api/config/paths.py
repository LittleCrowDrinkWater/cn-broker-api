"""配置文件的默认位置。**刻意在仓库之外**——见 `config/__init__.py`。"""
from __future__ import annotations

from pathlib import Path


#: 配置文件默认在仓库上一级（`cn_broker_api/config/paths.py` 往上三级），
#: **刻意不在仓库里**：配置里装的全是这台机器特有的东西。
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "cn-broker-api.toml"

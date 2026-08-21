"""配置文件去哪儿找。

## 查找顺序

1. 环境变量 `CN_BROKER_API_CONFIG`（显式指定，优先级最高）
2. `<仓库>/config.toml` —— **默认位置**。它在仓库目录里，但被 `.gitignore` 挡着
   （`*.toml` 全挡、只放行 `config.example.toml`）⇒ 真值不会被推上去。
3. `<仓库上一级>/cn-broker-api.toml` —— 旧位置，留着兼容已经这么配过的机器。

⚠️ 放在仓库目录里的代价只有一条：**`git clean -xdf` 会把它删掉**（未跟踪文件）。
所以别在这个仓库里跑那条命令。换来的是「配置和代码在一起、不用记第二个路径」。

⭐ 找不到任何一份**不是错**：全套默认值也起得来，只是 `tdx_home` 没给的话，
驱动会在第一次真调用时明确失败（而不是 import 期就把服务拖挂）。
"""
from __future__ import annotations

from pathlib import Path

#: 仓库根目录（`cn_broker_api/config/paths.py` 往上三级）。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: 默认位置：仓库里那份被 gitignore 挡着的真值。
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.toml"

#: 旧位置（仓库上一级）。**只用于兼容**，新装机器不必知道它。
LEGACY_CONFIG_PATH = REPO_ROOT.parent / "cn-broker-api.toml"


def candidates() -> tuple:
    """按优先级排的候选位置（不含环境变量那一路，那个由 loader 处理）。"""
    return (DEFAULT_CONFIG_PATH, LEGACY_CONFIG_PATH)

"""配置文件去哪儿找：环境变量 `CN_BROKER_API_CONFIG` > `<仓库>/config.toml` >
`<仓库上一级>/cn-broker-api.toml`（旧位置，只为兼容）。

⚠️ 默认位置在仓库目录里、靠 `.gitignore` 挡着 ⇒ 别在这个仓库跑 `git clean -xdf`。
⭐ 一份都找不到不是错：全套默认值起得来，只是 `tdx_home` 缺了会在第一次真调用时失败。
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

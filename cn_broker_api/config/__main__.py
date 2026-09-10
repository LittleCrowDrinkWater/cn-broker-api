"""`python -m cn_broker_api.config`：把生效的配置打出来；读不过就非零退出。

装计划任务之前必须先这么读一遍：任务跑的是 `pythonw.exe`（**没有控制台**），配置读不出来
那行 stderr 会被丢掉，能看见的只有「上次运行结果 = 2」。

⭐ **做成模块入口，而不是安装脚本里的 `python -c "..."`**：那种写法要在 PowerShell 里嵌一串
带引号的 Python 源码，引号被原生参数解析吃掉之后报的是 `SyntaxError` —— 看起来像配置坏了。
`install_task.ps1` 因此**从写出来到 2026-08-22 一次都没跑通过**，而这正是那个服务一直没被装成
计划任务的原因。
"""
from __future__ import annotations

import sys

from cn_broker_api.config import load
from cn_broker_api.config.error import ConfigError
from cn_broker_api.stdio import init_stdio


def main() -> int:
    init_stdio()
    try:
        cfg = load()
    except ConfigError as e:
        print(f"配置校验失败：{e}", file=sys.stderr)
        return 2
    print(f"配置来源: {cfg.source_path or '<defaults>'}")
    for key, value, given in cfg.describe():
        source = "configured" if given else "default"
        print(f"[{source:10}] {key:46} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

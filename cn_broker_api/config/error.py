"""配置缺失或不合法。"""
from __future__ import annotations



class ConfigError(Exception):
    """配置缺失或不合法。**可捕获的异常而不是 SystemExit**：入口要翻译成一句人话 +
    非零退出码，测试要能断言它，两种处置不该由本模块决定。"""

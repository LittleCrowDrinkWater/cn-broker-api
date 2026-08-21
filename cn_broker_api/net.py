"""出站 HTTP：**不读系统代理**。

本机系统代理在注册表里而 `requests` 默认会去读它，表现是「本机端口的请求莫名走了代理」，
而那种失败长得像「客户端没开」。⚠️ `trust_env = False` 同时关掉环境里的 CA bundle 与
netrc，本服务只打 `127.0.0.1`，两样都不用。

⭐ 与母项目 `backend/core/net.py` 是同一条规矩的两份实现，**刻意不共用**：
共用就得把母项目 import 进来，而本服务不该知道母项目存在。
"""
from __future__ import annotations

import requests


def direct_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s

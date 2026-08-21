"""HTTP 层的装配：建 app、注册各组路由。**这里不写业务**。

路由按关注点分在 `api/` 下，每组一个模块、各自一个 `register(app, ctx)`。

⭐ `cache` / `flight` / `queue` 由本函数各建**一份**再传下去，见 `api/context.py`。
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any, Optional

from flask import Flask

from cn_broker_api.api import (auth, diag_routes, health_routes, market_routes,
                               meta_routes, quote_routes, session_routes, trade_routes)
from cn_broker_api.api.context import ApiContext
from cn_broker_api.config import Config
from cn_broker_api.serial_queue import AccountSerialQueue
from cn_broker_api.singleflight import SingleFlight
from cn_broker_api.state import HealthCache, LastRun

logger = logging.getLogger(__name__)

#: 兼容旧的 import 路径：外面有代码 `from cn_broker_api.http_app import SingleFlight`。
__all__ = ["create_app", "load_or_create_token", "SingleFlight"]


def load_or_create_token(path: Path) -> str:
    """读 token，没有就生成一个并落盘（0600 语义靠目录权限，Windows 上尽力而为）。

    🔴 **没有 token 就不启动**是刻意的（fail closed）：一个无鉴权的本机端点，机器上任何
    进程、任何打开的网页都能打，而它能下单。自动生成让"第一次跑"不难受，但不放宽这条。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        tok = path.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    path.write_text(tok + "\n", encoding="utf-8")
    logger.warning("[auth] 已生成新 token：%s", path)
    return tok


def create_app(cfg: Config, driver: Any, *, token: Optional[str] = None,
               flight: Optional[SingleFlight] = None,
               watchdog: Any = None) -> Flask:
    """建 app。`flight` 可以从外面传进来：看门狗和 `/v1/session/ensure` **必须共用同一把**，
    各拿一把的表现是两个客户端进程。"""
    app = Flask(__name__, static_folder=None)
    app.config["CN_BROKER_API_CFG"] = cfg
    ctx = ApiContext(
        cfg=cfg, driver=driver,
        cache=HealthCache(ttl_seconds=cfg.health.cache_seconds),
        last_run=LastRun(state_dir=cfg.server.state_dir),
        flight=flight or SingleFlight(),
        queue=AccountSerialQueue(),
        token=token or load_or_create_token(cfg.token_file),
        repo_root=Path(__file__).resolve().parents[1],
        watchdog=watchdog)
    for group in (auth, meta_routes, health_routes, session_routes, trade_routes,
                  quote_routes, market_routes, diag_routes):
        group.register(app, ctx)
    return app

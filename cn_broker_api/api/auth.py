"""鉴权：三条，缺一不可。

① 只绑 `127.0.0.1`（在入口写死，不是配置项）② token 必需 ③ 校验 `Host` 头。

第三条容易被当成多余：针对 `127.0.0.1` 的 DNS 重绑定是真实手法——一个恶意网页可以让浏览器
把某个域名解析到 127.0.0.1，然后从页面里打这个端口。而**这个端口能下单**。
"""
from __future__ import annotations

import secrets

from flask import Flask, g, jsonify, request

from cn_broker_api.api.context import ApiContext

#: 允许的 Host 头。**不接受别的**——见模块 docstring 第③条。
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def register(app: Flask, ctx: ApiContext) -> None:
    cfg, tok = ctx.cfg, ctx.token

    @app.before_request
    def _guard():  # noqa: ANN202
        host = (request.host or "").split(":")[0]
        if host not in ALLOWED_HOSTS and f"[{host}]" not in ALLOWED_HOSTS:
            # DNS 重绑定：域名能解析到 127.0.0.1，但 Host 头会带着那个域名过来。
            return jsonify(error="bad_host",
                           message=f"Host {request.host!r} 不在放行名单里（防 DNS 重绑定）"), 421
        if request.path == "/" or request.path.startswith("/static/"):
            return None                      # 诊断页是静态的、不含数据，不鉴权
        if request.path == "/favicon.ico":
            # 浏览器自己会去要它。让它撞鉴权的话，console 里每次都多一条 401——
            # 而那条噪音会把真正该看见的报错淹掉。
            return ("", 204)
        got = (request.headers.get("Authorization") or "").strip()
        if not got.startswith("Bearer ") or not secrets.compare_digest(got[7:], tok):
            return jsonify(error="unauthorized",
                           message="要带 Authorization: Bearer <token>；"
                                   f"token 在 {cfg.token_file}"), 401
        g.authed = True
        return None

"""元信息、诊断页要的状态、诊断页本身。

契约版本这一个整数是跨仓库唯一的硬校验点：进程内的 import 在启动就炸，
HTTP 契约会在凌晨三点静默不对。
"""
from __future__ import annotations

from typing import Any, Dict

from flask import Flask, jsonify, request, send_from_directory

from cn_broker_api.api.context import ApiContext
from cn_broker_api.config import CONTRACT_VERSION


def register(app: Flask, ctx: ApiContext) -> None:
    cfg, driver, last_run = ctx.cfg, ctx.driver, ctx.last_run
    watchdog, repo_root = ctx.watchdog, ctx.repo_root

    @app.get("/v1/meta")
    def meta():  # noqa: ANN202
        """契约版本 + 驱动名 + 能力清单。**调用方启动时校 `contract`，不匹配直接拒跑。**
        进程内的 import 在启动就炸，HTTP 契约会在凌晨三点静默不对。"""
        return jsonify(contract=CONTRACT_VERSION, driver=getattr(driver, "name", "?"),
                       capabilities=driver.capabilities(),
                       cred_source=cfg.tdxquant.cred_source,
                       config_path=str(cfg.source_path) if cfg.source_path else None)

    @app.get("/v1/state")
    def state():  # noqa: ANN202
        """诊断页要的那几样。🔴 **绝不含密码**——只报"哪些账户手上有密码"。"""
        account = (request.args.get("account") or "").strip()
        vault = getattr(driver, "vault", None)
        latch = getattr(driver, "latch", None)
        return jsonify(
            contract=CONTRACT_VERSION,
            driver=getattr(driver, "name", "?"),
            capabilities=driver.capabilities(),
            cred_source=cfg.tdxquant.cred_source,
            submits_used=(latch.used(account) if latch else None),
            submits_max=(latch.max_per_day if latch else None),
            accounts_with_password=(vault.accounts_with_password() if vault else []),
            last_ensure=last_run.read(),
            watchdog=_watchdog_state(),
            config=[{"key": k, "value": v, "from_file": given}
                    for k, v, given in cfg.describe()],
        )

    def _watchdog_state() -> Dict[str, Any]:
        """看门狗那一块。⭐ **没开也要报**，而且要报"没开"这三个字：
        一个没在跑的看门狗和一个在跑但什么都没做的看门狗，在页面上不能长得一样。
        """
        out: Dict[str, Any] = {"enabled": cfg.watchdog.enabled,
                               "interval_seconds": cfg.watchdog.interval_seconds,
                               "window": f"{cfg.watchdog.window_start}~{cfg.watchdog.window_end}",
                               "weekdays_only": cfg.watchdog.weekdays_only,
                               "running": bool(watchdog is not None
                                               and getattr(watchdog, "_thread", None)
                                               is not None)}
        try:
            if watchdog is not None:
                out.update(watchdog.state.read())
                out["processes"] = driver.desktop_processes()
        except Exception as e:  # noqa: BLE001 — 诊断页不该因为一块读不出来整页空白
            out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return out

    @app.get("/")
    def index():  # noqa: ANN202
        return send_from_directory(repo_root, "status.html")

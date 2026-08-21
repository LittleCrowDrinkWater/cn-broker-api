"""把客户端弄到「交易通道可用」，以及查那一趟的进度。

⭐ 是 `ensure`（把它弄好，已经好了就什么都不做）而不是 `login`（去执行一次登录动作）：
状态驱动而非弹框驱动，所以定时任务可以放心反复打。
"""
from __future__ import annotations

import logging

from flask import Flask, jsonify, request

from cn_broker_api.api.context import ApiContext
from cn_broker_api.api.probe import cache_key as _cache_key
from cn_broker_api.api.probe import probe
from cn_broker_api.drivers.capability_missing import CapabilityMissing
from cn_broker_api.drivers.driver_error import DriverError
from cn_broker_api.state import SubmitBlocked

logger = logging.getLogger(__name__)


def register(app: Flask, ctx: ApiContext) -> None:
    driver, flight, last_run, cache = ctx.driver, ctx.flight, ctx.last_run, ctx.cache

    def _probe():  # noqa: ANN202
        return probe(driver)

    @app.post("/v1/session/ensure")
    def session_ensure():  # noqa: ANN202
        """把客户端弄到「交易通道可用」。**幂等 + 单飞**。

        ⭐ 是 `ensure`（把它弄好，已经好了就什么都不做）而不是 `login`（去执行一次登录动作）：
        状态驱动而非弹框驱动，所以定时任务可以放心反复打。
        """
        body = request.get_json(silent=True) or {}
        account = str(body.get("account") or "").strip()
        account_type = str(body.get("account_type") or "STOCK").strip().upper()
        password = body.get("password") or None
        wait = int(body.get("wait_seconds") or 240)
        start = bool(body.get("start", True))
        minimize = bool(body.get("minimize", True))

        job_id, mine = flight.start()
        if not mine:
            return jsonify(job_id=job_id, state="running",
                           message="已经有一趟在跑（单飞：重复调用不排队）"), 202
        try:
            res = driver.ensure_logged_in(password=password, account=account,
                                          account_type=account_type, start=start,
                                          minimize=minimize, wait_seconds=wait)
            payload = {"ok": res.ok, "acted": res.acted, "detail": res.detail}
            last_run.write(res.ok, res.detail)
            cache.put(_probe(), key=_cache_key())   # 登过之后缓存必然过期，顺手刷一次
            flight.finish(job_id, payload)
            return jsonify(job_id=job_id, state="done", **payload), (200 if res.ok else 503)
        except SubmitBlocked as e:
            # 🔴 **不是错误，是闸门生效了** ⇒ 409 而不是 5xx，且文案要能直接给人看。
            flight.finish(job_id, {"ok": False, "blocked": True, "detail": str(e)})
            return jsonify(job_id=job_id, state="done", ok=False, blocked=True,
                           error="submit_blocked", message=str(e)), 409
        except CapabilityMissing as e:
            flight.finish(job_id, {"ok": False, "detail": str(e)})
            return jsonify(job_id=job_id, ok=False,
                           error="capability_missing", message=str(e)), 501
        except DriverError as e:
            flight.finish(job_id, {"ok": False, "detail": str(e)})
            return jsonify(job_id=job_id, ok=False,
                           error="driver_error", message=str(e)), 503
        except Exception as e:  # noqa: BLE001
            logger.exception("[ensure] 未预期的失败")
            flight.finish(job_id, {"ok": False, "detail": f"{type(e).__name__}: {e}"})
            return jsonify(job_id=job_id, ok=False, error="internal",
                           message=f"{type(e).__name__}: {str(e)[:300]}"), 500

    @app.get("/v1/jobs/<job_id>")
    def job(job_id: str):  # noqa: ANN202
        got = flight.get(job_id)
        if got is None:
            return jsonify(error="not_found", message=f"没有任务 {job_id}"), 404
        return jsonify(got)

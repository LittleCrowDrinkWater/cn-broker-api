"""HTTP 层：契约的投影 + 鉴权 + 单飞。**与厂商无关**——它只跟 Driver 说话。

## 鉴权三条，缺一不可

① 只绑 `127.0.0.1`（在入口写死，不是配置项）② token 必需 ③ 校验 `Host` 头。

第三条容易被当成多余：针对 `127.0.0.1` 的 DNS 重绑定是真实手法——一个恶意网页可以让浏览器
把某个域名解析到 127.0.0.1，然后从页面里打这个端口。而**这个端口能下单**。

## 为什么 `/v1/health` 便宜、`/v1/health/refresh` 才真探

「交易账号登录了没」那一项要连客户端、查一次资产，几秒钟且占用串行槽。诊断页每 5 秒刷一次
的话，不缓存等于整天骚扰交易通道。

⇒ `GET` 读缓存并**必须回报年龄**（`age_seconds`）。静默展示旧数据的状态页比没有更糟。

## 单飞（single-flight）

`POST /v1/session/ensure` 可能跑几十秒。REST 天然诱发重复调用——客户端库重试、超时后重发、
监控周期打、浏览器刷新。所以：同一时刻只允许一趟在跑，后来的请求**不排队**，直接拿到正在
跑的那个任务号。
"""
from __future__ import annotations

import logging
import secrets
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from flask import Flask, g, jsonify, request, send_from_directory

from cn_broker_api.config import CONTRACT_VERSION, Config
from cn_broker_api.drivers.base import CapabilityMissing, DriverError
from cn_broker_api.state import HealthCache, LastRun, SubmitBlocked

logger = logging.getLogger(__name__)

#: 允许的 Host 头。**不接受别的**——见模块 docstring 第③条。
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")


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


class SingleFlight:
    """同一时刻只跑一趟。第二个请求**不排队**，拿到正在跑的那个任务号。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Optional[str] = None
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def start(self) -> Tuple[str, bool]:
        """返回 (任务号, 是不是本次新开的)。"""
        with self._lock:
            if self._current:
                return self._current, False
            job_id = uuid.uuid4().hex[:12]
            self._current = job_id
            self._jobs[job_id] = {"id": job_id, "state": "running",
                                  "started_at": datetime.now().isoformat(timespec="seconds")}
            return job_id, True

    def finish(self, job_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id) or {"id": job_id}
            job.update(payload)
            job["state"] = "done"
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            self._jobs[job_id] = job
            if self._current == job_id:
                self._current = None
            # 只留最近若干条：这是个运维端点，不是审计日志。
            if len(self._jobs) > 32:
                for k in sorted(self._jobs, key=lambda x: self._jobs[x].get("started_at", ""))[:8]:
                    self._jobs.pop(k, None)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._jobs.get(job_id)


def create_app(cfg: Config, driver: Any, *, token: Optional[str] = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["CN_BROKER_API_CFG"] = cfg
    tok = token or load_or_create_token(cfg.token_file)
    cache = HealthCache(ttl_seconds=cfg.health.cache_seconds)
    last_run = LastRun(state_dir=cfg.server.state_dir)
    flight = SingleFlight()
    repo_root = Path(__file__).resolve().parents[1]

    # ── 鉴权 ─────────────────────────────────────────────
    @app.before_request
    def _guard():  # noqa: ANN202
        host = (request.host or "").split(":")[0]
        if host not in ALLOWED_HOSTS and f"[{host}]" not in ALLOWED_HOSTS:
            # DNS 重绑定：域名能解析到 127.0.0.1，但 Host 头会带着那个域名过来。
            return jsonify(error="bad_host",
                           message=f"Host {request.host!r} 不在放行名单里（防 DNS 重绑定）"), 421
        if request.path == "/" or request.path.startswith("/static/"):
            return None                      # 诊断页是静态的、不含数据，不鉴权
        got = (request.headers.get("Authorization") or "").strip()
        if not got.startswith("Bearer ") or not secrets.compare_digest(got[7:], tok):
            return jsonify(error="unauthorized",
                           message="要带 Authorization: Bearer <token>；"
                                   f"token 在 {cfg.token_file}"), 401
        g.authed = True
        return None

    # ── 元信息 ───────────────────────────────────────────
    @app.get("/v1/meta")
    def meta():  # noqa: ANN202
        """契约版本 + 驱动名 + 能力清单。**调用方启动时校 `contract`，不匹配直接拒跑。**
        进程内的 import 在启动就炸，HTTP 契约会在凌晨三点静默不对。"""
        return jsonify(contract=CONTRACT_VERSION, driver=getattr(driver, "name", "?"),
                       capabilities=driver.capabilities(),
                       cred_source=cfg.tdxquant.cred_source,
                       config_path=str(cfg.source_path) if cfg.source_path else None)

    # ── 健康检查 ─────────────────────────────────────────
    def _probe() -> Dict[str, Any]:
        account = (request.args.get("account") or "").strip()
        account_type = (request.args.get("account_type") or "STOCK").strip().upper()
        try:
            return driver.health(account=account, account_type=account_type)
        except Exception as e:  # noqa: BLE001 — 自检自己就是兜底件，它崩了就什么都不知道了
            logger.exception("[health] 自检本身出错")
            return {"ok": False, "message": f"自检出错：{type(e).__name__}: {str(e)[:200]}",
                    "checks": []}

    @app.get("/v1/health")
    def health():  # noqa: ANN202
        """**便宜**：读缓存 + 回报年龄。没有缓存时才真探一次（第一次总得探）。"""
        e = cache.fresh()
        fresh_now = False
        if e is None:
            e = cache.put(_probe())
            fresh_now = True
        body = dict(e.payload)
        body["age_seconds"] = round(e.age_seconds(), 1)
        body["from_cache"] = not fresh_now
        body["cache_ttl_seconds"] = cache.ttl
        return jsonify(body)

    @app.post("/v1/health/refresh")
    def health_refresh():  # noqa: ANN202
        """强制重探。**要连客户端、占串行槽 ⇒ 只能按需打**（诊断页那个按钮）。"""
        e = cache.put(_probe())
        body = dict(e.payload)
        body["age_seconds"] = 0.0
        body["from_cache"] = False
        return jsonify(body)

    # ── 登录 ─────────────────────────────────────────────
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
            cache.put(_probe())          # 登过之后缓存必然过期，顺手刷一次
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

    # ── 诊断页与状态 ─────────────────────────────────────
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
        )

    @app.get("/")
    def index():  # noqa: ANN202
        return send_from_directory(repo_root, "status.html")

    return app

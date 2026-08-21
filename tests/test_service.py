"""服务层用例：鉴权、缓存、单飞、日闩。

⭐ 全部走纸面驱动 ⇒ **在任何平台都跑得过**，CI 靠它。真驱动那侧的用例见
`test_moved_judgments.py`（也全是假件，同样跨平台）。
"""
from __future__ import annotations

import pytest

from cn_broker_api.config import (CONTRACT_VERSION, Config, HealthConfig, ServerConfig,
                                  TdxQuantConfig)
from cn_broker_api.drivers.base import require
from cn_broker_api.drivers.capability import Capability
from cn_broker_api.drivers.capability_missing import CapabilityMissing
from cn_broker_api.drivers.paper import PaperDriver
from cn_broker_api.http_app import create_app
from cn_broker_api.state import PasswordVault, SubmitBlocked, SubmitLatch

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path):
    cfg = Config(server=ServerConfig(port=17710, state_dir=tmp_path),
                 health=HealthConfig(cache_seconds=30),
                 tdxquant=TdxQuantConfig(), driver="paper")
    return create_app(cfg, PaperDriver(), token=TOKEN).test_client()


# ── 鉴权：三条闸各自可分辨 ───────────────────────────────

def test_no_token_is_401(client):
    assert client.get("/v1/meta").status_code == 401


def test_wrong_token_is_401(client):
    assert client.get("/v1/meta", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_foreign_host_is_rejected(client):
    """针对 127.0.0.1 的 DNS 重绑定是真实手法：域名能解析到回环，但 Host 头会带着那个域名
    过来。而**这个端口能下单** ⇒ 必须按 Host 挡，不能只靠"只绑回环"。"""
    r = client.get("/v1/meta", headers={**AUTH, "Host": "evil.example.com"})
    assert r.status_code == 421


def test_favicon_is_204_not_401(client):
    """浏览器自己会去要 favicon。让它撞鉴权的话，console 里每次都多一条 401，
    而那条噪音会把真正该看见的报错淹掉。"""
    assert client.get("/favicon.ico").status_code == 204


def test_status_page_needs_no_token(client):
    """诊断页是静态的、不含任何数据 ⇒ 不鉴权。页面里的接口调用照样要 token。"""
    assert client.get("/").status_code == 200


# ── 契约版本：调用方靠它 fail loud ───────────────────────

def test_meta_reports_contract_driver_and_capabilities(client):
    b = client.get("/v1/meta", headers=AUTH).get_json()
    assert b["contract"] == CONTRACT_VERSION
    assert b["driver"] == "paper"
    assert Capability.CANCEL in b["capabilities"]


def test_paper_driver_does_not_claim_desktop_login():
    """⭐ 谎报能力会让「能力声明」这个机制失去意义——它存在的全部价值就是让调用方
    fail loud。纸面驱动确实登不了客户端，就不该声明它能。"""
    d = PaperDriver()
    assert Capability.DESKTOP_LOGIN not in d.capabilities()
    with pytest.raises(CapabilityMissing, match="capability|能力"):
        require(d, Capability.DESKTOP_LOGIN)


# ── 健康检查：便宜 + 必须回报年龄 ────────────────────────

def test_health_reports_age_and_serves_from_cache(client):
    """🔴 年龄必须回报：静默展示旧数据的状态页比没有更糟。
    🔴 第二次必须命中缓存：那一项要连客户端、占串行槽，页面每 5 秒轮询一次的话，
       不缓存等于整天骚扰交易通道。"""
    first = client.get("/v1/health", headers=AUTH).get_json()
    assert first["from_cache"] is False and first["age_seconds"] == pytest.approx(0, abs=1)
    second = client.get("/v1/health", headers=AUTH).get_json()
    assert second["from_cache"] is True


def test_refresh_always_probes(client):
    client.get("/v1/health", headers=AUTH)
    b = client.post("/v1/health/refresh", headers=AUTH).get_json()
    assert b["from_cache"] is False


def test_paper_health_marks_every_check_as_unverified(client):
    """纸面驱动四项恒绿，但**必须全部标 warn 并在 detail 里说明**——不能让人在诊断页上
    看着一排绿以为客户端真的通了。"""
    b = client.get("/v1/health", headers=AUTH).get_json()
    assert b["checks"] and all(c["warn"] for c in b["checks"])
    assert all("纸面" in c["detail"] for c in b["checks"])


# ── ensure：幂等 + 单飞 ──────────────────────────────────

def test_ensure_is_idempotent_and_reports_not_acted(client):
    """⭐ 是「把它弄好」而不是「执行一次登录」：已经好了就什么都不做（`acted=False`）。
    这就是定时任务能放心反复打的原因。"""
    for _ in range(3):
        b = client.post("/v1/session/ensure", json={}, headers=AUTH).get_json()
        assert b["ok"] is True and b["acted"] is False


def test_job_lookup_after_ensure(client):
    job = client.post("/v1/session/ensure", json={}, headers=AUTH).get_json()["job_id"]
    got = client.get(f"/v1/jobs/{job}", headers=AUTH).get_json()
    assert got["state"] == "done" and got["ok"] is True


def test_unknown_job_is_404(client):
    assert client.get("/v1/jobs/deadbeef", headers=AUTH).status_code == 404


# ── 日闩：怕丢掉，所以落盘 ──────────────────────────────

def test_latch_blocks_the_second_submit_of_the_day(tmp_path):
    latch = SubmitLatch(tmp_path, max_per_day=1)
    latch.claim("acct")
    with pytest.raises(SubmitBlocked, match="今天已提交过"):
        latch.claim("acct")


def test_latch_survives_a_restart(tmp_path):
    """🔴 这一条是它必须落盘的全部理由：放内存的话重启就清零，
    「重启三次＝无声无息地试了三次密码」。"""
    SubmitLatch(tmp_path, max_per_day=1).claim("acct")
    assert SubmitLatch(tmp_path, max_per_day=1).remaining("acct") == 0


def test_latch_counts_per_account(tmp_path):
    latch = SubmitLatch(tmp_path, max_per_day=1)
    latch.claim("a")
    assert latch.remaining("b") == 1        # 别的账户不受影响


def test_unreadable_latch_is_treated_as_used_up(tmp_path):
    """坏文件的两种解释里，保守那一边的代价是今天人工登一次；乐观那一边的代价是多试密码。"""
    latch = SubmitLatch(tmp_path, max_per_day=1)
    latch.claim("acct")
    bad = next(tmp_path.glob("latch-*.json"))
    bad.write_text("{ 这不是 JSON", encoding="utf-8")
    assert latch.remaining("acct") == 0
    with pytest.raises(SubmitBlocked, match="坏了"):
        latch.claim("acct")


# ── 密码：怕留下来，所以只在内存 ─────────────────────────

def test_vault_never_exposes_the_password(tmp_path):
    """诊断页要显示"哪些账户手上有密码"，那就**只能**是账户名。"""
    v = PasswordVault()
    v.put("acct", "s3cret")
    assert v.accounts_with_password() == ["acct"]
    assert "s3cret" not in str(v.accounts_with_password())


def test_vault_entry_expires_with_the_day(tmp_path):
    from datetime import date, timedelta

    v = PasswordVault()
    v.put("acct", "s3cret", day=date.today() - timedelta(days=1))
    assert v.get("acct") is None            # 昨天下发的不在今天被悄悄复用


def test_state_endpoint_carries_no_password(tmp_path):
    """验的是**密码值**不外泄，不是"响应里不出现 password 这个词"——字段名
    `accounts_with_password` 本身就含那个词，按词禁只会得到一条什么都焊不住的用例。"""
    cfg = Config(server=ServerConfig(port=17710, state_dir=tmp_path),
                 health=HealthConfig(cache_seconds=30),
                 tdxquant=TdxQuantConfig(), driver="paper")
    drv = PaperDriver()
    drv.vault = PasswordVault()
    drv.vault.put("acct", "s3cret-value")
    drv.latch = SubmitLatch(tmp_path, max_per_day=1)
    c = create_app(cfg, drv, token=TOKEN).test_client()
    body = c.get("/v1/state?account=acct", headers=AUTH).get_json()
    assert "s3cret-value" not in str(body)
    assert body["accounts_with_password"] == ["acct"]     # 只报账户名
    assert body["submits_used"] == 0 and body["submits_max"] == 1

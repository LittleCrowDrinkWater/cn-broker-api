"""直接 HQMP 登录编排只负责接线，密码与窗口判据仍由既有模块独家实现。"""
from __future__ import annotations

from argparse import Namespace

from cn_broker_api import tdx_hqmp_direct as cli


def _args(tmp_path):
    return Namespace(
        call_timeout=20.0,
        connect_timeout=120.0,
        cred_file=tmp_path / "cred.json",
        login_state_dir=tmp_path / "state",
        max_password_submits_per_day=10,
        max_consecutive_failures=3,
        keep_login_window=False,
    )


def test_already_ready_session_does_not_read_credentials(tmp_path, monkeypatch):
    class ReadyHost:
        @staticmethod
        def channel_ok(timeout):
            return True, "ready"

    monkeypatch.setattr(
        cli.desktop_login,
        "load_cred",
        lambda: (_ for _ in ()).throw(AssertionError("不应读取凭证")),
    )

    cli._ensure_direct_login(ReadyHost(), _args(tmp_path))


def test_direct_login_claims_and_settles_password_budget(tmp_path, monkeypatch):
    events = []

    class WaitingHost:
        @staticmethod
        def channel_ok(timeout):
            return False, "not ready"

    class FakeLatch:
        def __init__(self, state_dir, max_per_day, max_consecutive_failures):
            events.append(("latch", state_dir, max_per_day, max_consecutive_failures))

        def claim(self, account):
            events.append(("claim", account))

        def settle(self, account, ok):
            events.append(("settle", account, ok))

    monkeypatch.setattr(cli, "SubmitLatch", FakeLatch)
    monkeypatch.setattr(cli.desktop_login, "set_cred_path", lambda path: events.append(("cred", path)))
    monkeypatch.setattr(
        cli.desktop_login,
        "load_cred",
        lambda: {"account": "private", "password": "secret", "account_type": "CREDIT"},
    )

    def ensure(cred, **kwargs):
        events.append(("ensure", kwargs["start"], kwargs["required_processes"]))
        assert kwargs["channel_probe"](cred) == (False, "not ready")
        return True, "direct ready"

    monkeypatch.setattr(cli.desktop_login, "ensure_logged_in", ensure)

    cli._ensure_direct_login(WaitingHost(), _args(tmp_path))

    assert events == [
        ("cred", tmp_path / "cred.json"),
        ("latch", tmp_path / "state", 10, 3),
        ("claim", "private"),
        ("ensure", False, ("TC.exe",)),
        ("settle", "private", True),
    ]

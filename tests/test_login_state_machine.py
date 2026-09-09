"""桌面登录循环的密码提交次数是资金安全约束。"""
from __future__ import annotations

from cn_broker_api.drivers.tdxquant import login


def test_injected_channel_probe_can_confirm_login(monkeypatch):
    monkeypatch.setattr(login, "_WIN", True)
    monkeypatch.setattr(login, "_target_pids", lambda names: {7: "TC.exe"})
    seen = []

    result = login.ensure_logged_in(
        {"account": "private", "password": "secret"},
        start=False,
        required_processes=("TC.exe",),
        channel_probe=lambda cred: (seen.append(cred) or True, "direct ready"),
    )

    assert result == (True, "direct ready")
    assert len(seen) == 1


def test_trade_password_is_submitted_only_once_while_dialog_remains(monkeypatch):
    monkeypatch.setattr(login, "_WIN", True)
    monkeypatch.setattr(login, "_target_pids", lambda names: {7: "TC.exe"})
    monkeypatch.setattr(login, "find_login_dialog", lambda pids: 99)
    monkeypatch.setattr(login, "snapshot", lambda dialog: [])
    monkeypatch.setattr(login, "classify", lambda controls: "trade")
    monkeypatch.setattr(login.time, "sleep", lambda seconds: None)
    times = iter((0.0, 1.0, 4.0))
    monkeypatch.setattr(login.time, "time", lambda: next(times))
    submits = []
    monkeypatch.setattr(
        login,
        "_do_trade_login",
        lambda dialog, controls, cred: submits.append(dialog) or True,
    )

    ok, _ = login.ensure_logged_in(
        {"account": "private", "password": "secret"},
        wait=3,
        start=False,
        minimize=False,
        required_processes=("TC.exe",),
        channel_probe=lambda cred: (False, "not ready"),
    )

    assert ok is False
    assert submits == [99]

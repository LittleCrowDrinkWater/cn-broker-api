"""交易会话状态必须用确切证据分类，不能把所有通道错误都猜成未登录。"""
from __future__ import annotations

from cn_broker_api.config import TdxQuantConfig
from cn_broker_api.drivers.session_state import SessionState
from cn_broker_api.drivers.tdxquant import driver as driver_module
from cn_broker_api.drivers.tdxquant.driver import TdxQuantDriver
from cn_broker_api.state import PasswordVault, SubmitLatch


def _driver(tmp_path):
    return TdxQuantDriver(
        TdxQuantConfig(),
        latch=SubmitLatch(tmp_path),
        vault=PasswordVault(),
    )


def test_session_is_ready_only_after_account_and_asset_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(driver_module.L, "channel_ok", lambda cred: (True, "含资产金额的详情"))

    status = _driver(tmp_path).session_status(account="private", account_type="CREDIT")

    assert status == {
        "state": SessionState.READY.value,
        "ready": True,
        "detail": "交易账户和资产查询已通过",
    }
    assert "private" not in str(status)
    assert "资产金额" not in str(status)


def test_missing_trade_kernel_is_explicitly_login_required(tmp_path, monkeypatch):
    monkeypatch.setattr(driver_module.L, "channel_ok", lambda cred: (False, "not ready"))
    monkeypatch.setattr(driver_module.L, "_target_pids", lambda names: {})

    status = _driver(tmp_path).session_status()

    assert status["state"] == SessionState.LOGIN_REQUIRED.value
    assert status["ready"] is False


def test_trade_login_dialog_is_explicitly_login_required(tmp_path, monkeypatch):
    monkeypatch.setattr(driver_module.L, "channel_ok", lambda cred: (False, "not ready"))
    monkeypatch.setattr(driver_module.L, "_target_pids", lambda names: {7: "TC.exe"})
    monkeypatch.setattr(driver_module.L, "find_login_dialog", lambda pids: 99)
    monkeypatch.setattr(driver_module.L, "snapshot", lambda dialog: [])
    monkeypatch.setattr(driver_module.L, "classify", lambda controls: "trade")

    status = _driver(tmp_path).session_status()

    assert status["state"] == SessionState.LOGIN_REQUIRED.value


def test_running_kernel_without_login_evidence_is_channel_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(driver_module.L, "channel_ok", lambda cred: (False, "ambiguous"))
    monkeypatch.setattr(driver_module.L, "_target_pids", lambda names: {7: "TC.exe"})
    monkeypatch.setattr(driver_module.L, "find_login_dialog", lambda pids: None)

    status = _driver(tmp_path).session_status()

    assert status == {
        "state": SessionState.CHANNEL_UNAVAILABLE.value,
        "ready": False,
        "detail": "交易内核运行中，但账户与资产查询未通过，且没有明确的登录窗口",
    }

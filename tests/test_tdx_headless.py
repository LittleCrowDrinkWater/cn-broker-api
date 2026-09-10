from pathlib import Path

import pytest

from cn_broker_api.config import ConfigError, load
from cn_broker_api.config.tdxquant import TdxQuantConfig
from cn_broker_api.drivers.tdxquant import headless
from cn_broker_api.drivers.tdxquant import driver as driver_module
from cn_broker_api.drivers.tdxquant.driver import TDX_HEADLESS_RECIPE, TdxQuantDriver
from cn_broker_api.drivers.tdxquant.hqmp_direct_trading import HqmpDirectTrading
from cn_broker_api.drivers.driver_error import DriverError


def _lab_root(tmp_path: Path, *, http: int = 1) -> Path:
    required = (
        ".trade-lab-marker",
        "TdxCopilot.dll",
        "PYPlugins/TPyth.dll",
        "PYPlugins/tdxRpcx64.dll",
        "NewTc/TC.exe",
    )
    for relative in required:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (tmp_path / "TMTconfig.ini").write_text(
        "[HQMP]\nPort=13575\n[PYMP]\nPort=14572\n"
        f"HTTP={http}\n[MCP]\nPort=17711\n",
        encoding="ascii",
    )
    return tmp_path


def test_lab_marker_and_all_runtime_files_are_required(tmp_path):
    with pytest.raises(RuntimeError, match="实验副本缺少必要文件"):
        headless.require_lab_root(tmp_path)
    root = _lab_root(tmp_path)
    assert headless.require_lab_root(root) == root.resolve()


def test_ports_come_from_tmtconfig_and_must_enable_http(tmp_path):
    assert headless.read_ports(_lab_root(tmp_path)) == (13575, 14572, 17711)
    (tmp_path / "TMTconfig.ini").write_text(
        "[HQMP]\nPort=13575\n[PYMP]\nPort=14572\nHTTP=0\n[MCP]\nPort=17711\n",
        encoding="ascii",
    )
    with pytest.raises(RuntimeError, match="HTTP 必须为 1"):
        headless.read_ports(tmp_path)


def test_headless_mode_requires_mcp(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[driver.tdxquant]\ndesktop_mode='headless'\ntransport='ctypes'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="transport 必须是 mcp"):
        load(config)


def test_hqmp_transport_requires_headless_port_and_capture(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[driver.tdxquant]\ntransport='hqmp'\ndesktop_mode='full'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="desktop_mode 必须是 headless"):
        load(config)

    config.write_text(
        "[driver.tdxquant]\ntransport='hqmp'\ndesktop_mode='headless'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="hqmp_port"):
        load(config)


def test_valid_hqmp_settings_are_loaded_without_touching_the_client(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[driver.tdxquant]\n"
        "transport='hqmp'\n"
        "desktop_mode='headless'\n"
        "hqmp_port=13575\n"
        "hqmp_capture='outside.jsonl'\n"
        "hqmp_enable_trade=true\n"
        "hqmp_max_order_size=100\n"
        "hqmp_max_order_notional=2000\n",
        encoding="utf-8",
    )

    cfg = load(config).tdxquant

    assert cfg.transport == "hqmp"
    assert cfg.hqmp_port == 13575
    assert cfg.hqmp_capture == Path("outside.jsonl")
    assert cfg.hqmp_enable_trade is True


class _FakeDirectSession:
    def __init__(self, root, port, capture, *, enable_trade=False):
        self.root = root
        self.port = port
        self.capture = capture
        self.enable_trade = enable_trade
        self.started = []
        self.stopped = False
        self.registration_resets = 0

    def start(self, **kwargs):
        self.started.append(kwargs)

    def stop(self):
        self.stopped = True

    def channel_ok(self, _timeout):
        return True, "HQMP 账户和资产查询已通过"

    def require_account(self, _account, *, timeout):
        return None

    def registration_snapshot(self):
        return True, False

    def reset_unregistered_connection(self):
        self.registration_resets += 1
        return True

    def verified_target_pids(self, names):
        return {7: "TC.exe"}


def test_hqmp_driver_starts_listener_and_never_adds_tdxw(tmp_path, monkeypatch):
    (tmp_path / ".trade-lab-marker").touch()
    capture = tmp_path / "outside.jsonl"
    monkeypatch.setattr(driver_module, "HqmpDirectSession", _FakeDirectSession)
    driver = TdxQuantDriver(
        TdxQuantConfig(
            tdx_home=tmp_path,
            desktop_mode="headless",
            transport="hqmp",
            hqmp_port=13575,
            hqmp_capture=capture,
            hqmp_enable_trade=True,
        ),
        latch=object(),
    )

    assert driver.desktop_recipe().processes == ("TC.exe",)
    assert "Tdxw.exe" not in driver.desktop_recipe().executables
    assert driver._hqmp_session.started == [{"launch_tc": False, "reuse_tc": False}]
    assert isinstance(driver.trading(account_type="CREDIT"), HqmpDirectTrading)
    assert "credit_order" in driver.capabilities()
    assert "market_data" not in driver.capabilities()

    driver.close()
    assert driver._hqmp_session.stopped is True


def test_ready_hqmp_login_does_not_read_credentials(tmp_path, monkeypatch):
    (tmp_path / ".trade-lab-marker").touch()
    monkeypatch.setattr(driver_module, "HqmpDirectSession", _FakeDirectSession)
    driver = TdxQuantDriver(
        TdxQuantConfig(
            tdx_home=tmp_path,
            desktop_mode="headless",
            transport="hqmp",
            hqmp_port=13575,
            hqmp_capture=tmp_path / "outside.jsonl",
        ),
        latch=object(),
    )
    monkeypatch.setattr(
        driver,
        "_resolve_cred",
        lambda **_kwargs: pytest.fail("已就绪时不应读取或解密凭据"),
    )

    settled = []
    driver.latch = type(
        "_Latch",
        (),
        {"settle": lambda _self, account, ok: settled.append((account, ok))},
    )()

    got = driver.ensure_logged_in(account="private-account", account_type="CREDIT")

    assert got.ok is True
    assert got.acted is False
    assert settled == [("private-account", True)]


def test_hqmp_login_resets_a_stuck_unregistered_connection_once(tmp_path, monkeypatch):
    (tmp_path / ".trade-lab-marker").touch()
    monkeypatch.setattr(driver_module, "HqmpDirectSession", _FakeDirectSession)
    driver = TdxQuantDriver(
        TdxQuantConfig(
            tdx_home=tmp_path,
            desktop_mode="headless",
            transport="hqmp",
            hqmp_port=13575,
            hqmp_capture=tmp_path / "outside.jsonl",
            cred_source="request",
        ),
        latch=type(
            "_Latch",
            (),
            {
                "claim": lambda _self, _account: None,
                "settle": lambda _self, _account, _ok: None,
            },
        )(),
    )
    monkeypatch.setattr(driver, "_direct_channel_probe", lambda _account: (False, "not ready"))
    times = iter((0.0, 6.0, 9.0))
    monkeypatch.setattr(driver_module.time, "monotonic", lambda: next(times))

    def ensure_logged_in(_cred, **kwargs):
        probe = kwargs["channel_probe"]
        assert kwargs["process_finder"] == driver._hqmp_session.verified_target_pids
        assert probe({}) == (False, "not ready")
        assert "已重置 HQMP 连接" in probe({})[1]
        ready, detail = probe({})
        assert ready is False
        assert detail == "not ready；本轮已尝试重置一次未注册的 HQMP 连接"
        return False, "still not ready"

    monkeypatch.setattr(driver_module.L, "ensure_logged_in", ensure_logged_in)

    got = driver.ensure_logged_in(
        password="secret", account="private-account", account_type="CREDIT"
    )

    assert got.ok is False
    assert driver._hqmp_session.registration_resets == 1


def test_hqmp_status_rejects_a_same_name_process_from_another_install(tmp_path, monkeypatch):
    (tmp_path / ".trade-lab-marker").touch()
    monkeypatch.setattr(driver_module, "HqmpDirectSession", _FakeDirectSession)
    driver = TdxQuantDriver(
        TdxQuantConfig(
            tdx_home=tmp_path,
            desktop_mode="headless",
            transport="hqmp",
            hqmp_port=13575,
            hqmp_capture=tmp_path / "outside.jsonl",
        ),
        latch=object(),
    )
    monkeypatch.setattr(driver, "_direct_channel_probe", lambda _account: (False, "not ready"))
    monkeypatch.setattr(
        driver._hqmp_session,
        "verified_target_pids",
        lambda _names: (_ for _ in ()).throw(RuntimeError("路径不匹配")),
    )

    status = driver.session_status(account_type="CREDIT")

    assert status["state"] == "CHANNEL_UNAVAILABLE"
    assert status["ready"] is False
    assert "RuntimeError" in status["detail"]


def test_hqmp_status_retries_a_delayed_registration_then_returns_ready(tmp_path, monkeypatch):
    (tmp_path / ".trade-lab-marker").touch()
    monkeypatch.setattr(driver_module, "HqmpDirectSession", _FakeDirectSession)
    driver = TdxQuantDriver(
        TdxQuantConfig(
            tdx_home=tmp_path,
            desktop_mode="headless",
            transport="hqmp",
            hqmp_port=13575,
            hqmp_capture=tmp_path / "outside.jsonl",
        ),
        latch=object(),
    )
    probes = iter(((False, "TC 尚未注册"), (True, "账户和资产查询已通过")))
    registrations = iter(((True, False), (True, False), (True, True)))
    monkeypatch.setattr(driver, "_direct_channel_probe", lambda _account: next(probes))
    monkeypatch.setattr(
        driver._hqmp_session, "registration_snapshot", lambda: next(registrations)
    )
    monkeypatch.setattr(driver_module.L, "find_login_dialog", lambda _pids: None)
    monkeypatch.setattr(driver_module.time, "monotonic", lambda: 0.0)
    sleeps = []
    monkeypatch.setattr(driver_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    status = driver.session_status(account_type="CREDIT")

    assert status == {
        "state": "READY",
        "ready": True,
        "detail": "账户和资产查询已通过",
    }
    assert sleeps == [0.5, 0.5]


def test_hqmp_status_does_not_retry_while_login_dialog_is_visible(tmp_path, monkeypatch):
    (tmp_path / ".trade-lab-marker").touch()
    monkeypatch.setattr(driver_module, "HqmpDirectSession", _FakeDirectSession)
    driver = TdxQuantDriver(
        TdxQuantConfig(
            tdx_home=tmp_path,
            desktop_mode="headless",
            transport="hqmp",
            hqmp_port=13575,
            hqmp_capture=tmp_path / "outside.jsonl",
        ),
        latch=object(),
    )
    monkeypatch.setattr(driver, "_direct_channel_probe", lambda _account: (False, "not ready"))
    monkeypatch.setattr(driver_module.L, "find_login_dialog", lambda _pids: 99)
    monkeypatch.setattr(driver_module.L, "snapshot", lambda _dialog: [])
    monkeypatch.setattr(driver_module.L, "classify", lambda _controls: "trade")
    monkeypatch.setattr(
        driver,
        "_retry_direct_registration",
        lambda *_args: pytest.fail("登录框存在时不应等待 RegisterClient"),
    )

    status = driver.session_status(account_type="CREDIT")

    assert status["state"] == "LOGIN_REQUIRED"
    assert status["ready"] is False


def test_hqmp_status_stops_retrying_after_registration_deadline(tmp_path, monkeypatch):
    (tmp_path / ".trade-lab-marker").touch()
    monkeypatch.setattr(driver_module, "HqmpDirectSession", _FakeDirectSession)
    driver = TdxQuantDriver(
        TdxQuantConfig(
            tdx_home=tmp_path,
            desktop_mode="headless",
            transport="hqmp",
            hqmp_port=13575,
            hqmp_capture=tmp_path / "outside.jsonl",
        ),
        latch=object(),
    )
    monkeypatch.setattr(driver, "_direct_channel_probe", lambda _account: (False, "TC 尚未注册"))
    monkeypatch.setattr(driver_module.L, "find_login_dialog", lambda _pids: None)
    monkeypatch.setattr(
        driver._hqmp_session, "registration_snapshot", lambda: (True, False)
    )
    times = iter((0.0, 0.0, 10.0))
    monkeypatch.setattr(driver_module.time, "monotonic", lambda: next(times))
    sleeps = []
    monkeypatch.setattr(driver_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    status = driver.session_status(account_type="CREDIT")

    assert status["state"] == "CHANNEL_UNAVAILABLE"
    assert status["ready"] is False
    assert status["detail"] == "TC 尚未注册；已等待 10 秒仍未注册"
    assert sleeps == [0.5]


def test_headless_driver_recipe_never_contains_tdxw(tmp_path):
    (tmp_path / ".trade-lab-marker").touch()
    driver = TdxQuantDriver(
        TdxQuantConfig(tdx_home=tmp_path, desktop_mode="headless"), latch=object()
    )
    assert driver.desktop_recipe() == TDX_HEADLESS_RECIPE
    assert driver.desktop_recipe().processes == ("TC.exe",)
    assert "Tdxw.exe" not in driver.desktop_recipe().executables


def test_headless_driver_refuses_an_unmarked_installation(tmp_path):
    with pytest.raises(DriverError, match="只能使用带 .trade-lab-marker"):
        TdxQuantDriver(
            TdxQuantConfig(tdx_home=tmp_path, desktop_mode="headless"), latch=object()
        )


def test_preflight_runs_all_guards_without_starting_dlls(tmp_path, monkeypatch):
    root = _lab_root(tmp_path)
    seen = []
    monkeypatch.setattr(headless, "_running_trade_processes", lambda: [])
    monkeypatch.setattr(headless, "_verify_ports_available", lambda ports: seen.append(ports))
    monkeypatch.setattr(
        headless, "_verify_lab_routing", lambda got_root, port: seen.append((got_root, port))
    )
    assert headless.preflight(root) == (root.resolve(), 13575, 14572, 17711)
    assert seen == [(13575, 14572, 17711), (root.resolve(), 13575)]

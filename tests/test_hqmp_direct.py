import ctypes
import json

import pytest

from cn_broker_api.drivers.tdxquant.hqmp_direct import (
    HqmpDirectSession,
    _RpcString,
    _response_for,
    _selected_account,
)
from cn_broker_api.trade.credit_kind import CreditOrderKind


class _FakeRpc:
    def __init__(self, host):
        self.host = host
        self.calls = []

    def callRpcClientInterfaceByToken(self, interface, request_ptr, client_token_ptr):
        request = ctypes.cast(request_ptr, ctypes.POINTER(_RpcString)).contents
        client_token = ctypes.cast(client_token_ptr, ctypes.POINTER(_RpcString)).contents
        body = json.loads(ctypes.string_at(request.data, request.length))
        route = ctypes.string_at(client_token.data, client_token.length)
        self.calls.append((interface, route, body))
        response = {
            "method": "ReturnValueComp",
            "params": {
                "functionname": body["method"],
                "token": body["params"]["token"],
                "value": [{"safe": "result"}],
            },
        }
        self.host._handle_request(json.dumps(response).encode())
        return True


def test_responses_match_the_observed_callback_contract():
    assert json.loads(_response_for("RegisterClient")) == {"resultType": "int", "result": 1}
    assert json.loads(_response_for("GetInjectHwnd")) == {
        "resultType": "HWND",
        "result": "0000000000000000",
    }
    assert json.loads(_response_for("NotifyMsgClient")) == {"resultType": "nullptr"}
    assert json.loads(_response_for("RawExternSwitch")) == {"resultType": "long", "result": 0}


def test_registers_client_and_correlates_a_read_only_callback(tmp_path):
    host = HqmpDirectSession(tmp_path, 13575)
    registration = {
        "method": "RegisterClient",
        "params": {"token": "private-route-token", "terminalId": ""},
    }
    assert json.loads(host._handle_request(json.dumps(registration).encode()))["result"] == 1
    assert host._client_registered.is_set()
    assert not host._client_ready.is_set()
    ready = {"method": "NotifyMsgClient", "params": {"MsgType": "1"}}
    host._handle_request(json.dumps(ready).encode())
    assert host._client_ready.is_set()
    host._rpc = _FakeRpc(host)
    result = host.call("OperateUser_0", {})
    assert result == [{"safe": "result"}]
    interface, route, body = host._rpc.calls[0]
    assert interface == b"callfunction"
    assert route == b"private-route-token"
    assert body["method"] == "OperateUser_0"
    assert body["params"]["token"] == "private-route-token"


def test_trade_methods_require_the_explicit_gate(tmp_path):
    host = HqmpDirectSession(tmp_path, 13575)
    host._rpc = _FakeRpc(host)
    host._client_token = "route"
    with pytest.raises(ValueError, match="交易闸"):
        host.call("DoLevinGN_909", {"zqdm": "000001"})
    assert host._rpc.calls == []


def test_selects_the_current_account_without_exposing_its_values():
    selected = {"issel": "1", "qsid": "broker", "zjzh": "account"}
    assert _selected_account([{"issel": "0"}, selected]) is selected
    assert _selected_account([selected]) is selected
    with pytest.raises(RuntimeError, match="唯一"):
        _selected_account([{"issel": "0"}, {"issel": "0"}])


def test_probe_derives_query_parameters_from_the_selected_account(tmp_path, monkeypatch):
    host = HqmpDirectSession(tmp_path, 13575)
    calls = []

    def call(method, params, timeout=20.0):
        calls.append((method, params, timeout))
        if method == "OperateUser_0":
            return [{"issel": "1", "qsid": "broker", "zjzh": "account"}]
        return []

    monkeypatch.setattr(host, "call", call)
    assert host.probe(3.0) == {
        "account_count": 1,
        "position_count": 0,
        "asset_row_count": 0,
        "order_count": 0,
    }
    assert calls == [
        ("DoLevinGN_927", {"mode": "1", "semauto": "1"}, 3.0),
        ("OperateUser_0", {}, 3.0),
        ("DoLevinGN_803", {"qsid": "broker", "szID": "", "zjzh": "account", "zqdm": ""}, 3.0),
        ("DoLevinGN_830", {"qsid": "broker", "szID": "", "zjzh": "account"}, 3.0),
        ("DoLevinGN_807", {}, 3.0),
    ]


def test_builds_the_observed_collateral_buy_contract(tmp_path, monkeypatch):
    constants = tmp_path / "PYPlugins" / "sys"
    constants.mkdir(parents=True)
    (constants / "tqcenter.py").write_text(
        "class tqconst:\n"
        "    STOCK_BUY = 0\n"
        "    STOCK_SELL = 1\n"
        "    CREDIT_BUY = 0\n"
        "    PRICE_MY = 0\n",
        encoding="utf-8",
    )
    host = HqmpDirectSession(tmp_path, 13575, enable_trade=True)
    calls = []
    monkeypatch.setattr(host, "_current_account", lambda timeout: {"userid": "private-user"})

    def call(method, params, timeout=20.0):
        calls.append((method, params, timeout))
        return [{"retflag": "1", "retinfo": "", "wtbh": "private-order"}]

    monkeypatch.setattr(host, "call", call)
    assert host.place_order(
        symbol="000001.SZ",
        security_name="平安银行",
        side="buy",
        size=100,
        price=10.72,
        credit_kind=CreditOrderKind.COLLATERAL_BUY,
        timeout=3.0,
    ) == {"order_id": "private-order", "message": ""}
    assert calls == [
        (
            "DoLevinGN_909",
            {
                "szID": "private-user",
                "qsid": "0",
                "realzjzh": "",
                "zqdm": "000001",
                "zqmc": "平安银行",
                "setcode": "0",
                "bsflag": "0",
                "flag": "0",
                "price": "10.720000",
                "nwtfs": "0",
                "wtsl": "100",
                "bwaitans": "1",
            },
            3.0,
        )
    ]


def test_builds_the_observed_cancel_contract(tmp_path, monkeypatch):
    host = HqmpDirectSession(tmp_path, 13575, enable_trade=True)
    calls = []
    monkeypatch.setattr(host, "_current_account", lambda timeout: {"zjzh": "private-account"})

    def call(method, params, timeout=20.0):
        calls.append((method, params, timeout))
        return [{"success": "true", "errmsg": ""}]

    monkeypatch.setattr(host, "call", call)
    assert host.cancel_order(order_id="private-order", timeout=3.0) == {
        "order_id": "private-order",
        "message": "",
    }
    assert calls == [
        (
            "DoLevinGN_808",
            {"wtbh": "private-order", "zjzh": "private-account"},
            3.0,
        )
    ]


@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf")])
def test_rejects_invalid_prices_before_account_lookup(tmp_path, monkeypatch, price):
    host = HqmpDirectSession(tmp_path, 13575, enable_trade=True)
    looked_up = False

    def current_account(timeout):
        nonlocal looked_up
        looked_up = True
        return {}

    monkeypatch.setattr(host, "_current_account", current_account)
    with pytest.raises(ValueError, match="限价"):
        host.place_order(
            symbol="000001",
            security_name="平安银行",
            side="buy",
            size=100,
            price=price,
        )
    assert not looked_up

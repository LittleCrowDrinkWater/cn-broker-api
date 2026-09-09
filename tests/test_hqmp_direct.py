import json
import socket
import threading

import pytest

from cn_broker_api.drivers.tdxquant.hqmp_capture import decode_body, encode_body, extract_body
from cn_broker_api.drivers.tdxquant.hqmp_direct import (
    HqmpDirectSession,
    _encode_call,
    _json_response,
    _pack_length,
    _response_for,
    _selected_account,
)
from cn_broker_api.trade.credit_kind import CreditOrderKind


def _call_frame(value, key, guid=b"A" * 36, request_id=0):
    body = encode_body(value, key)
    payload = b"\x93\x00\xd9\x30callfunction" + guid + _pack_length(len(body)) + body
    return (
        b"\x27\x01\x00\x00"
        + len(payload).to_bytes(4, "little")
        + request_id.to_bytes(4, "little")
        + b"\0" * 8
        + payload
    )


def _response_template():
    payload = b'\x92\x00\xd9\x02{}\n'
    return b"\x27\x00\x00\x00" + len(payload).to_bytes(4, "little") + b"\0" * 12 + payload


def _rpc_frame(value, key, request_id):
    body = encode_body(value, key)
    payload = b"\x92\xabrpcfunction" + _pack_length(len(body)) + body
    return (
        b"\x27\x00\x00\x00"
        + len(payload).to_bytes(4, "little")
        + request_id.to_bytes(4, "little")
        + b"\0" * 8
        + payload
    )


def _recv_frame(connection):
    header = b""
    while len(header) < 20:
        header += connection.recv(20 - len(header))
    payload = b""
    size = int.from_bytes(header[4:8], "little")
    while len(payload) < size:
        payload += connection.recv(size - len(payload))
    return header + payload


def test_responses_match_the_observed_callback_contract():
    assert _response_for("RegisterClient") == {"resultType": "int", "result": "1"}
    assert _response_for("GetInjectHwnd") == {
        "resultType": "HWND",
        "result": "0000000000000000",
    }
    assert _response_for("NotifyMsgClient") == {"resultType": "nullptr"}
    assert _response_for("RawExternSwitch") == {"resultType": "long", "result": "0"}


def test_encodes_a_call_with_the_live_connection_guid():
    pytest.importorskip("Crypto.Cipher.Blowfish")
    key = b"synthetic-test-key"
    template = _call_frame({"method": "Old", "params": {}}, key, b"A" * 36)
    value = {"method": "Query", "returnType": "", "params": {"token": "private"}}
    frame = _encode_call(template, value, key, b"B" * 36)
    assert b"B" * 36 in frame
    assert b"A" * 36 not in frame
    assert int.from_bytes(frame[4:8], "little") == len(frame) - 20
    assert int.from_bytes(frame[8:12], "little") == 0
    assert decode_body(extract_body(frame), key) == value


def test_builds_plain_response_with_the_incoming_request_id():
    frame = _json_response(
        _response_template(), 37, {"resultType": "int", "result": "1"}
    )
    assert int.from_bytes(frame[8:12], "little") == 37
    payload = frame[20:]
    assert payload[:3] == b"\x92\x00\xd9"
    length = payload[3]
    assert json.loads(payload[4:4 + length]) == {"resultType": "int", "result": "1"}


def test_correlates_return_value_using_the_special_account_token(tmp_path, monkeypatch):
    host = HqmpDirectSession(tmp_path, 13575, tmp_path / "capture.jsonl")
    host._client_token = "private-route-token"
    sent = []

    def send_call(method, params):
        sent.append((method, params))
        host._key = b"synthetic-test-key"
        host._response_template = _response_template()
        host._connection = type("Socket", (), {"sendall": lambda self, frame: None})()
        callback = {
            "method": "ReturnValueComp",
            "params": {
                "functionname": method,
                "token": params["token"],
                "value": [{"safe": "result"}],
            },
        }
        payload = b"\x92\xabrpcfunction" + _pack_length(len(encode_body(callback, host._key)))
        payload += encode_body(callback, host._key)
        frame = b"\x27\x00\x00\x00" + len(payload).to_bytes(4, "little") + b"\0" * 12 + payload
        host._handle_frame(frame)

    monkeypatch.setattr(host, "_send_call", send_call)
    assert host.call("OperateUser_0", {}) == [{"safe": "result"}]
    assert sent == [("OperateUser_0", {"token": "private-route-token"})]


def test_socket_session_closes_registration_call_and_callback_loop(tmp_path):
    pytest.importorskip("Crypto.Cipher.Blowfish")
    key = b"synthetic-test-key"
    guid = b"B" * 36
    host = HqmpDirectSession(tmp_path, 13575, tmp_path / "capture.jsonl")
    host._key = key
    host._response_template = _response_template()
    host._templates = {
        "DoLevinGN_927": _call_frame({"method": "Old", "params": {}}, key)
    }
    server_connection, client_connection = socket.socketpair()
    client_connection.settimeout(2)
    worker = threading.Thread(
        target=host._serve_connection, args=(server_connection,), daemon=True
    )
    worker.start()
    try:
        guid_payload = b"\x92\xaccallfunction\xd9\x24" + guid
        guid_frame = (
            b"\x27\x01\x00\x00"
            + len(guid_payload).to_bytes(4, "little")
            + b"\0" * 12
            + guid_payload
        )
        register = {
            "method": "RegisterClient",
            "params": {"token": "private-route-token", "terminalId": ""},
        }
        client_connection.sendall(guid_frame + _rpc_frame(register, key, 1))
        assert int.from_bytes(_recv_frame(client_connection)[8:12], "little") == 1
        ready = {"method": "NotifyMsgClient", "params": {"MsgType": "1"}}
        client_connection.sendall(_rpc_frame(ready, key, 2))
        assert int.from_bytes(_recv_frame(client_connection)[8:12], "little") == 2
        host.wait_for_client(1)

        result = []
        call_worker = threading.Thread(
            target=lambda: result.append(
                host.call("DoLevinGN_927", {"mode": "1", "semauto": "1"}, 1)
            ),
            daemon=True,
        )
        call_worker.start()
        request = _recv_frame(client_connection)
        decoded = decode_body(extract_body(request), key)
        assert decoded["method"] == "DoLevinGN_927"
        assert decoded["params"] == {"mode": "1", "semauto": "1", "token": "927"}
        assert guid in request
        callback = {
            "method": "ReturnValueComp",
            "params": {"functionname": "DoLevinGN_927", "token": "927", "value": []},
        }
        client_connection.sendall(_rpc_frame(callback, key, 3))
        assert int.from_bytes(_recv_frame(client_connection)[8:12], "little") == 3
        call_worker.join(timeout=2)
        assert not call_worker.is_alive()
        assert result == [[]]
    finally:
        host._stop_event.set()
        client_connection.close()
        worker.join(timeout=2)
        server_connection.close()


def test_wait_for_client_can_accept_registered_reused_session(tmp_path):
    host = HqmpDirectSession(tmp_path, 13575, tmp_path / "capture.jsonl")
    host._client_registered.set()

    host.wait_for_client(0.01, require_ready=False)


def test_trade_methods_require_the_explicit_gate(tmp_path):
    host = HqmpDirectSession(tmp_path, 13575, tmp_path / "capture.jsonl")
    host._client_token = "route"
    with pytest.raises(ValueError, match="交易闸"):
        host.call("DoLevinGN_909", {"zqdm": "000001"})


def test_selects_the_current_account_without_exposing_its_values():
    selected = {"issel": "1", "qsid": "broker", "zjzh": "account"}
    assert _selected_account([{"issel": "0"}, selected]) is selected
    assert _selected_account([selected]) is selected
    with pytest.raises(RuntimeError, match="唯一"):
        _selected_account([{"issel": "0"}, {"issel": "0"}])


def test_probe_replays_bootstrap_with_a_dynamically_selected_account(tmp_path, monkeypatch):
    host = HqmpDirectSession(tmp_path, 13575, tmp_path / "capture.jsonl")
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
        ("DoLevinGN_809", {"flag": "5", "setcode": "0", "qsid": "0", "zqdm": "", "zjzh": ""}, 3.0),
        ("DoLevinGN_809", {"flag": "6", "setcode": "0", "qsid": "0", "zqdm": "", "zjzh": ""}, 3.0),
        ("DoLevinGN_807", {}, 3.0),
        ("DoLevinGN_822", {"setcode": "-1", "wtbh": "", "zqdm": ""}, 3.0),
        ("DoLevinGN_822", {"setcode": "-1", "wtbh": "", "zqdm": ""}, 3.0),
        ("DoLevinGN_920", {}, 3.0),
        ("DoLevinGN_803", {"qsid": "broker", "szID": "", "zjzh": "account", "zqdm": ""}, 3.0),
        ("DoLevinGN_830", {"qsid": "broker", "szID": "", "zjzh": "account"}, 3.0),
        ("DoLevinGN_822", {"setcode": "-1", "wtbh": "", "zqdm": ""}, 3.0),
    ]


def test_channel_ok_requires_registration(tmp_path):
    host = HqmpDirectSession(tmp_path, 13575, tmp_path / "capture.jsonl")

    assert host.channel_ok() == (False, "TC 尚未注册到直接 HQMP 宿主")


def test_channel_ok_uses_account_and_asset_fields(tmp_path, monkeypatch):
    host = HqmpDirectSession(tmp_path, 13575, tmp_path / "capture.jsonl")
    host._client_registered.set()
    host._client_token = "route"
    account = {"qsid": "broker", "zjzh": "account"}
    monkeypatch.setattr(host, "_initialize_account", lambda timeout: ([account], account))
    calls = []

    def call(method, params, timeout=20.0):
        calls.append((method, params, timeout))
        return [{"keyong": "0.00"}] if method == "DoLevinGN_830" else []

    monkeypatch.setattr(host, "call", call)

    assert host.channel_ok(3.0) == (True, "HQMP 账户和资产查询已通过")
    assert calls[-1] == (
        "DoLevinGN_830",
        {"qsid": "broker", "szID": "", "zjzh": "account"},
        3.0,
    )


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
    host = HqmpDirectSession(
        tmp_path, 13575, tmp_path / "capture.jsonl", enable_trade=True
    )
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
    host = HqmpDirectSession(
        tmp_path, 13575, tmp_path / "capture.jsonl", enable_trade=True
    )
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
        ("DoLevinGN_808", {"wtbh": "private-order", "zjzh": "private-account"}, 3.0)
    ]


@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf")])
def test_rejects_invalid_prices_before_account_lookup(tmp_path, monkeypatch, price):
    host = HqmpDirectSession(
        tmp_path, 13575, tmp_path / "capture.jsonl", enable_trade=True
    )
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

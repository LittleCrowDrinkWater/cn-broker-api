import json
import socket
import threading

import pytest

from cn_broker_api.drivers.tdxquant import hqmp_direct as hqmp_module
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
from cn_broker_api.trade.ack_unknown import AckUnknown
from cn_broker_api.trade.order_rejected import OrderRejected
from cn_broker_api.trade.query_unavailable import QueryUnavailable


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


def _direct_order(**changes):
    row = {
        "wtbh": "private-order",
        "zqdm": "000001",
        "setcode": "0",
        "bsflag": "0",
        "wtsl": "100",
        "wtjg": "10.72",
        "cjsl": "0",
        "cjjg": "0",
        "cdflag": "0",
        "kcdflag": "1",
        "wtsj": "93000",
        "ztsm": "买入@正常委托@",
    }
    row.update(changes)
    return row


def test_query_order_uses_the_later_822_observation(tmp_path, monkeypatch):
    host = HqmpDirectSession(tmp_path, 13575, tmp_path / "capture.jsonl")
    calls = []

    def call(method, params, timeout=20.0):
        calls.append((method, params, timeout))
        if method == "DoLevinGN_807":
            return [_direct_order()]
        return [_direct_order(cdflag="1", kcdflag="0", ztsm="买入@已撤@")]

    monkeypatch.setattr(host, "call", call)

    got = host.query_order(order_id="private-order", timeout=3.0)

    assert host._direct_order_state(got) == "canceled"
    assert [(method, params) for method, params, _timeout in calls] == [
        ("DoLevinGN_807", {
            "setcode": "-1", "wtbh": "private-order", "zqdm": "",
        }),
        ("DoLevinGN_822", {
            "setcode": "-1", "wtbh": "private-order", "zqdm": "",
        }),
    ]
    assert all(0 < timeout <= 3.0 for _method, _params, timeout in calls)


def test_cancel_waits_until_the_order_is_visible_and_cancellable(tmp_path, monkeypatch):
    host = HqmpDirectSession(
        tmp_path, 13575, tmp_path / "capture.jsonl", enable_trade=True
    )
    observations = iter([
        None,
        _direct_order(),
        _direct_order(cdflag="1", kcdflag="0", ztsm="买入@已撤@"),
    ])
    cancels = []
    monkeypatch.setattr(host, "query_order", lambda **kw: next(observations))
    monkeypatch.setattr(
        host,
        "cancel_order",
        lambda **kw: cancels.append(kw) or {"order_id": kw["order_id"], "message": ""},
    )

    got = host.cancel_order_and_wait(
        order_id="private-order", visibility_timeout=1.0,
        settle_timeout=1.0, interval=0.0, call_timeout=3.0,
    )

    assert got["outcome"] == "canceled" and got["canceled"] is True
    assert got["order"]["status"] == "canceled"
    assert got["order"]["order_time"] == "093000"
    assert cancels == [{"order_id": "private-order", "timeout": 3.0}]


def test_cancel_retries_once_only_after_a_fresh_cancellable_observation(tmp_path, monkeypatch):
    host = HqmpDirectSession(
        tmp_path, 13575, tmp_path / "capture.jsonl", enable_trade=True
    )
    observations = iter([
        _direct_order(),
        _direct_order(),
        _direct_order(cdflag="1", kcdflag="0", ztsm="买入@已撤@"),
    ])
    attempts = []
    monkeypatch.setattr(host, "query_order", lambda **kw: next(observations))

    def cancel_order(**kw):
        attempts.append(kw)
        if len(attempts) == 1:
            raise OrderRejected("TC 拒绝撤单", broker_message="没有对应的委托信息")
        return {"order_id": kw["order_id"], "message": "撤单已报"}

    monkeypatch.setattr(host, "cancel_order", cancel_order)

    got = host.cancel_order_and_wait(
        order_id="private-order", visibility_timeout=1.0,
        settle_timeout=1.0, interval=0.0, call_timeout=3.0,
    )

    assert got["outcome"] == "canceled"
    assert len(attempts) == 2


def test_cancel_does_not_retry_without_a_fresh_cancellable_observation(tmp_path, monkeypatch):
    host = HqmpDirectSession(
        tmp_path, 13575, tmp_path / "capture.jsonl", enable_trade=True
    )
    query_count = 0
    attempts = []
    clock = iter(i / 10 for i in range(100))
    monkeypatch.setattr(hqmp_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(hqmp_module.time, "sleep", lambda _seconds: None)

    def query_order(**_kwargs):
        nonlocal query_count
        query_count += 1
        return _direct_order() if query_count == 1 else None

    def cancel_order(**kwargs):
        attempts.append(kwargs)
        raise OrderRejected("TC 拒绝撤单", broker_message="没有对应的委托信息")

    monkeypatch.setattr(host, "query_order", query_order)
    monkeypatch.setattr(host, "cancel_order", cancel_order)

    got = host.cancel_order_and_wait(
        order_id="private-order", visibility_timeout=1.0,
        settle_timeout=1.0, interval=0.0, call_timeout=3.0,
    )

    assert got["outcome"] == "timeout"
    assert len(attempts) == 1


def test_cancel_ack_timeout_only_reconciles_and_never_resubmits(tmp_path, monkeypatch):
    host = HqmpDirectSession(
        tmp_path, 13575, tmp_path / "capture.jsonl", enable_trade=True
    )
    observations = iter([
        _direct_order(),
        _direct_order(cdflag="1", kcdflag="0", ztsm="买入@已撤@"),
    ])
    attempts = []
    monkeypatch.setattr(host, "query_order", lambda **kw: next(observations))

    def cancel_order(**kw):
        attempts.append(kw)
        raise AckUnknown("撤单回调超时")

    monkeypatch.setattr(host, "cancel_order", cancel_order)

    got = host.cancel_order_and_wait(
        order_id="private-order", visibility_timeout=1.0,
        settle_timeout=1.0, interval=0.0, call_timeout=3.0,
    )

    assert got["outcome"] == "canceled"
    assert len(attempts) == 1


def test_filled_order_is_not_sent_to_cancel(tmp_path, monkeypatch):
    host = HqmpDirectSession(
        tmp_path, 13575, tmp_path / "capture.jsonl", enable_trade=True
    )
    monkeypatch.setattr(
        host, "query_order", lambda **kw: _direct_order(cjsl="100", kcdflag="0")
    )
    monkeypatch.setattr(
        host, "cancel_order", lambda **kw: pytest.fail("已成交委托不应再发送撤单"),
    )

    got = host.cancel_order_and_wait(order_id="private-order")

    assert got["outcome"] == "filled" and got["canceled"] is False


def test_partial_fill_followed_by_cancel_keeps_the_filled_quantity(tmp_path, monkeypatch):
    host = HqmpDirectSession(
        tmp_path, 13575, tmp_path / "capture.jsonl", enable_trade=True
    )
    observations = iter([
        _direct_order(cjsl="30"),
        _direct_order(cjsl="30", cdflag="1", kcdflag="0", ztsm="买入@已撤@"),
    ])
    monkeypatch.setattr(host, "query_order", lambda **kw: next(observations))
    monkeypatch.setattr(
        host, "cancel_order", lambda **kw: {"order_id": kw["order_id"], "message": ""},
    )

    got = host.cancel_order_and_wait(order_id="private-order", interval=0.0)

    assert got["outcome"] == "canceled"
    assert got["order"]["status"] == "canceled"
    assert got["order"]["filled_size"] == "30"


def test_cancel_rejects_a_symbol_mismatch_before_sending(tmp_path, monkeypatch):
    host = HqmpDirectSession(
        tmp_path, 13575, tmp_path / "capture.jsonl", enable_trade=True
    )
    monkeypatch.setattr(host, "query_order", lambda **kw: _direct_order())
    monkeypatch.setattr(
        host, "cancel_order", lambda **kw: pytest.fail("代码不匹配时不应发送撤单"),
    )

    with pytest.raises(ValueError, match="不一致"):
        host.cancel_order_and_wait(
            order_id="private-order", symbol="600000.SH", interval=0.0
        )


def test_direct_queries_use_the_dynamically_selected_account(tmp_path, monkeypatch):
    host = HqmpDirectSession(tmp_path, 13575, tmp_path / "capture.jsonl")
    monkeypatch.setattr(
        host,
        "_current_account",
        lambda timeout: {"qsid": "private-broker", "zjzh": "private-account"},
    )
    calls = []

    def call(method, params, timeout=20.0):
        calls.append((method, params, timeout))
        return []

    monkeypatch.setattr(host, "call", call)

    assert host.query_orders(timeout=3.0) == []
    assert host.query_positions(timeout=3.0) == []
    assert host.query_assets(timeout=3.0) == []
    assert calls == [
        ("DoLevinGN_807", {}, 3.0),
        (
            "DoLevinGN_803",
            {
                "qsid": "private-broker",
                "szID": "",
                "zjzh": "private-account",
                "zqdm": "",
            },
            3.0,
        ),
        (
            "DoLevinGN_830",
            {"qsid": "private-broker", "szID": "", "zjzh": "private-account"},
            3.0,
        ),
    ]


def test_direct_account_guard_does_not_echo_account_numbers(tmp_path, monkeypatch):
    host = HqmpDirectSession(tmp_path, 13575, tmp_path / "capture.jsonl")
    monkeypatch.setattr(host, "_current_account", lambda timeout: {"zjzh": "actual-secret"})

    with pytest.raises(ValueError) as got:
        host.require_account("requested-secret", timeout=3.0)

    message = str(got.value)
    assert "actual-secret" not in message
    assert "requested-secret" not in message


def test_empty_requested_account_explicitly_accepts_the_selected_default(tmp_path, monkeypatch):
    host = HqmpDirectSession(tmp_path, 13575, tmp_path / "capture.jsonl")
    monkeypatch.setattr(
        host, "_current_account", lambda timeout: pytest.fail("默认账户不需额外查询")
    )

    host.require_account("", timeout=3.0)


@pytest.mark.parametrize("method", ["query_orders", "query_positions", "query_assets"])
def test_direct_queries_do_not_turn_a_malformed_response_into_an_empty_book(
    tmp_path, monkeypatch, method
):
    host = HqmpDirectSession(tmp_path, 13575, tmp_path / "capture.jsonl")
    monkeypatch.setattr(
        host, "_current_account", lambda timeout: {"qsid": "broker", "zjzh": "account"}
    )
    monkeypatch.setattr(host, "call", lambda *_args, **_kwargs: None)

    with pytest.raises(QueryUnavailable):
        getattr(host, method)(timeout=3.0)

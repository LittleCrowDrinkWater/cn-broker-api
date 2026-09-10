"""直接 HQMP 的 TradingPort 适配层测试；全部使用假会话。"""
from __future__ import annotations

import pytest

from cn_broker_api.drivers.driver_error import DriverError
from cn_broker_api.drivers.tdxquant.hqmp_direct_trading import HqmpDirectTrading
from cn_broker_api.trade.ack_unknown import AckUnknown
from cn_broker_api.trade.credit_kind import CreditOrderKind
from cn_broker_api.trade.query_unavailable import QueryUnavailable


class _FakeSession:
    def __init__(self):
        self.enable_trade = True
        self.placed = []
        self.canceled = []
        self.order = None
        self.orders = []
        self.positions = []
        self.assets = []
        self.required_accounts = []

    def require_account(self, account, **kwargs):
        self.required_accounts.append((account, kwargs))

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return {"order_id": "private-order", "message": ""}

    def cancel_order_and_wait(self, **kwargs):
        self.canceled.append(kwargs)
        return {"canceled": True, "outcome": "canceled", "order": None, "reason": "done"}

    def query_order(self, **_kwargs):
        return self.order

    def query_orders(self, **_kwargs):
        return self.orders

    def query_positions(self, **_kwargs):
        return self.positions

    def query_assets(self, **_kwargs):
        return self.assets

    @staticmethod
    def _direct_order_row(row):
        return {"order_id": row["wtbh"], "symbol": f"{row['zqdm']}.SZ"}


def _trading(session=None, *, account="", account_type="CREDIT", instrument=None, **kwargs):
    session = session or _FakeSession()
    instrument = instrument if instrument is not None else {"name": "平安银行"}
    return session, HqmpDirectTrading(
        session,
        account=account,
        account_type=account_type,
        instrument_of=lambda _code: instrument,
        call_timeout=3.0,
        cancel_visibility_timeout=1.0,
        cancel_confirm_timeout=2.0,
        cancel_confirm_interval=0.1,
        **kwargs,
    )


def test_create_order_resolves_the_verified_security_name_before_sending():
    session, trading = _trading()

    row = trading.create_order(
        symbol="000001",
        side="buy",
        size=100,
        price=10.61,
        client_order_id="client-1",
        credit_kind=CreditOrderKind.COLLATERAL_BUY,
    )

    assert row["order_id"] == "private-order"
    assert row["client_order_id"] == "client-1"
    assert row["symbol"] == "000001.SZ"
    assert session.placed == [{
        "symbol": "000001.SZ",
        "security_name": "平安银行",
        "side": "buy",
        "size": 100,
        "price": 10.61,
        "credit_kind": CreditOrderKind.COLLATERAL_BUY,
        "timeout": 3.0,
    }]
    assert session.required_accounts == [("", {"timeout": 3.0})]


def test_missing_security_name_uses_symbol_as_the_only_order_identity():
    session, trading = _trading(instrument={"name": ""})

    trading.create_order(symbol="000001", side="buy", size=100, price=10.61)

    assert session.placed[0]["symbol"] == "000001.SZ"
    assert session.placed[0]["security_name"] == ""


@pytest.mark.parametrize("size,price", [(200, 10.61), (100, 20.01)])
def test_order_risk_limits_fail_before_account_or_name_lookup(size, price):
    session, trading = _trading()

    with pytest.raises(ValueError, match="单笔上限"):
        trading.create_order(symbol="000001", side="buy", size=size, price=price)

    assert session.required_accounts == []
    assert session.placed == []


def test_credit_order_on_a_stock_account_fails_before_name_lookup():
    looked_up = False
    session = _FakeSession()

    def instrument_of(_code):
        nonlocal looked_up
        looked_up = True
        return {"name": "平安银行"}

    trading = HqmpDirectTrading(
        session, account_type="STOCK", instrument_of=instrument_of
    )
    with pytest.raises(ValueError, match="STOCK"):
        trading.create_order(
            symbol="000001",
            side="buy",
            size=100,
            price=10.61,
            credit_kind=CreditOrderKind.FIN_BUY,
        )

    assert looked_up is False
    assert session.placed == []


def test_direct_order_rejects_the_page_confirmation_option():
    session, trading = _trading()

    with pytest.raises(ValueError, match="notify"):
        trading.create_order(
            symbol="000001", side="buy", size=100, price=10.61, notify=0
        )

    assert session.placed == []


def test_closed_trade_gate_fails_before_account_or_name_lookup():
    session, trading = _trading()
    session.enable_trade = False

    with pytest.raises(DriverError, match="交易闸未打开"):
        trading.create_order(symbol="000001", side="buy", size=100, price=10.61)

    assert session.required_accounts == []
    assert session.placed == []


def test_cancel_passes_symbol_and_reliable_wait_settings_to_the_session():
    session, trading = _trading()

    result = trading.cancel_order(symbol="000001", order_id="private-order")

    assert result["outcome"] == "canceled"
    assert session.canceled == [{
        "symbol": "000001.SZ",
        "order_id": "private-order",
        "visibility_timeout": 1.0,
        "settle_timeout": 2.0,
        "interval": 0.1,
        "call_timeout": 3.0,
    }]


def test_query_rows_map_only_measured_hqmp_fields():
    session, trading = _trading()
    session.positions = [{
        "zqdm": "000001",
        "setcode": "0",
        "zqsl": "500",
        "kmsl": "400",
        "cbj": "10.25",
        "zxj": "10.61",
        "fdyk": "180.00",
    }]
    session.assets = [{
        "zican": "200000.00",
        "keyong": "120000.00",
        "yu": "120000.00",
        "yk": "180.00",
    }]

    positions = trading.get_positions()
    account = trading.get_account()

    assert positions == [{
        "symbol": "000001.SZ",
        "size": "500",
        "avg_price": "10.25",
        "mark_price": "10.61",
        "unrealized_pnl": "180.00",
        "sellable": "400",
    }]
    assert trading.get_sellable() == {"000001.SZ": "400"}
    assert account["total_equity"] == "200000.00"
    assert account["total_available"] == "120000.00"
    assert account["total_unrealized_pnl"] == "180.00"


def test_multiple_asset_rows_are_unavailable_not_arbitrarily_selected():
    session, trading = _trading()
    session.assets = [{"zican": "1"}, {"zican": "2"}]

    with pytest.raises(QueryUnavailable, match="多条"):
        trading.get_account()


def test_unexpected_transport_failure_becomes_driver_error():
    session, trading = _trading()

    def broken(**_kwargs):
        raise RuntimeError("disconnected")

    session.place_order = broken
    with pytest.raises(DriverError, match="通道不可用"):
        trading.create_order(symbol="000001", side="buy", size=100, price=10.61)


def test_ack_unknown_is_not_downgraded_to_channel_unavailable():
    session, trading = _trading()

    def unknown(**_kwargs):
        raise AckUnknown("报单回调超时")

    session.place_order = unknown
    with pytest.raises(AckUnknown):
        trading.create_order(symbol="000001", side="buy", size=100, price=10.61)


def test_quote_capability_fails_loudly_without_a_market_port():
    _session, trading = _trading()

    with pytest.raises(DriverError, match="未验证行情"):
        trading.quotes(["000001"])
    assert trading.instrument("000001") == {"name": "平安银行"}


def test_requested_account_is_checked_before_a_trade():
    session, trading = _trading(account="expected-account")

    trading.create_order(symbol="000001", side="buy", size=100, price=10.61)

    assert session.required_accounts == [("expected-account", {"timeout": 3.0})]

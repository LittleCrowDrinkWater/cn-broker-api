"""真驱动交易那侧的判据。全走假客户端 ⇒ 跨平台，且**打不到柜台**。"""
from __future__ import annotations

import pytest

from cn_broker_api.drivers.driver_error import DriverError
from cn_broker_api.drivers.tdxquant.client import TdxQuantConnectionError
from cn_broker_api.drivers.tdxquant.trading import TdxQuantTrading
from cn_broker_api.trade.ack_unknown import AckUnknown
from cn_broker_api.trade.credit_kind import CreditOrderKind
from cn_broker_api.trade.order_pending_confirm import OrderPendingConfirm
from cn_broker_api.trade.order_rejected import OrderRejected
from cn_broker_api.trade.query_unavailable import QueryUnavailable


class _Const:
    PRICE_MY = 0
    STOCK_BUY, STOCK_SELL = 0, 1
    CREDIT_FIN_BUY, CREDIT_STK_REPAY = 69, 76


class _FakeClient:
    """假客户端。`orders` 每次被读走一项，用来演「撤单期间状态在变」。"""

    def __init__(self, *, account_type="STOCK", order_res=None, orders=None,
                 positions=None, asset=None, info=None, snapshot=None):
        self.account_type = account_type
        self.tqconst = _Const()
        self.order_res = order_res or {"ErrorId": "0", "Wtbh": "88"}
        self.orders = list(orders or [])
        self.positions = positions
        self.asset = asset if asset is not None else {"Asset": 1.0, "Cash": 1.0}
        self.info = info
        self.snapshot = snapshot
        self.sent = []
        self.cancels = []
        self._last_orders = []

    def connect(self):
        return None

    def order_stock(self, code, order_type, volume, price_type, price, **kw):
        self.sent.append((code, order_type, volume, price_type, price, kw))
        return self.order_res

    def cancel(self, code, order_id):
        self.cancels.append((code, order_id))

    def query_orders(self, _code=""):
        # 排空之后停在最后那个状态：真柜台不会因为我们多问一次就变回来。
        if self.orders:
            self._last_orders = self.orders.pop(0)
        return self._last_orders

    def query_positions(self):
        return self.positions

    def query_asset(self):
        return self.asset

    def query_stock_info(self, _code):
        return self.info

    def query_snapshot(self, _code):
        return self.snapshot


def _trading(client, **kw):
    kw.setdefault("cancel_timeout", 0.05)
    kw.setdefault("cancel_interval", 0.0)
    return TdxQuantTrading(client, **kw)


# ---- 报单三态 ----

def test_pending_confirm_comes_from_the_message():
    c = _FakeClient(order_res={"ErrorId": "0", "Msg": "已发送信号至客户端，待用户确认！"})
    with pytest.raises(OrderPendingConfirm):
        _trading(c).create_order(symbol="000761", side="buy", size=100, price=4.8)


def test_pending_confirm_also_comes_from_value_1_without_an_order_id():
    """`Value` 在部分版本上是字符串、也可能整个字段缺失 ⇒ 两个判据都要。"""
    c = _FakeClient(order_res={"ErrorId": "0", "Value": "1"})
    with pytest.raises(OrderPendingConfirm):
        _trading(c).create_order(symbol="000761", side="buy", size=100, price=4.8)


def test_a_reject_is_a_reject():
    c = _FakeClient(order_res={"ErrorId": "2", "Msg": "(143050001)非融资品标的买入[603230]"})
    with pytest.raises(OrderRejected):
        _trading(c).create_order(symbol="603230", side="buy", size=100, price=9.9)


def test_credit_order_needs_a_credit_account():
    """普通账户报 69 号回来的是一句看不懂的柜台码 ⇒ 在发出**之前**就拒。"""
    with pytest.raises(DriverError, match="CREDIT"):
        _trading(_FakeClient()).create_order(symbol="000761", side="buy", size=100,
                                             price=4.8,
                                             credit_kind=CreditOrderKind.FIN_BUY)


def test_credit_kind_must_match_the_side():
    """把「卖券还款」报成买入是无意义的委托，而正T 两条腿共用一段代码、方向是变量。"""
    c = _FakeClient(account_type="CREDIT")
    with pytest.raises(DriverError, match="方向"):
        _trading(c).create_order(symbol="000761", side="buy", size=100, price=4.8,
                                 credit_kind=CreditOrderKind.STK_REPAY)


def test_credit_kind_maps_to_the_vendor_number():
    c = _FakeClient(account_type="CREDIT")
    _trading(c).create_order(symbol="000761", side="buy", size=100, price=4.8,
                             credit_kind=CreditOrderKind.FIN_BUY)
    assert c.sent[0][1] == 69


def test_notify_is_not_sent_unless_asked():
    """生产路径一律不传：实测传 0 时实盘账户照样回「待用户确认」。"""
    c = _FakeClient()
    _trading(c).create_order(symbol="000761", side="buy", size=100, price=4.8)
    assert c.sent[0][5] == {}


# ---- 撤单与成交在赛跑 ----

def _order_row(**kw):
    row = {"Wtbh": "88", "Code": "000761.SZ", "WtVol": 100, "CjVol": 0,
           "WtPrice": 4.8, "CjPrice": 0, "BSFlag": 0, "Status": 1}
    row.update(kw)
    return row


def test_cancel_loses_the_race_and_says_so():
    """🔴 「我发了撤单」与「这笔没成交」是两件事。实测撤单中部分成交 1300/20900 股。"""
    c = _FakeClient(orders=[[_order_row()], [_order_row(CjVol=100, Status=3)]])
    res = _trading(c).cancel_order(symbol="000761", order_id="88")
    assert res["canceled"] is False and "成交" in res["reason"]


def test_cancel_confirmed_when_the_order_leaves_the_book():
    c = _FakeClient(orders=[[]])
    res = _trading(c).cancel_order(symbol="000761", order_id="88")
    assert res["canceled"] is True


def test_a_query_blip_during_cancel_is_not_a_confirmed_cancel():
    """🔴 查询抖一下返回 None 时把这一格判成已撤，调用方随即重下 ⇒ 双份成交。
    判不了就继续等，等到超时按未确认交回。"""
    c = _FakeClient(orders=[None, None, None])
    res = _trading(c).cancel_order(symbol="000761", order_id="88")
    assert res["canceled"] is False and "没等到已撤" in res["reason"]


def test_cancel_is_actually_submitted():
    c = _FakeClient(orders=[[]])
    _trading(c).cancel_order(symbol="000761", order_id="88")
    assert c.cancels == [("000761.SZ", "88")]


# ---- 查询三态 ----

def test_positions_none_is_unavailable_not_empty():
    with pytest.raises(QueryUnavailable):
        _trading(_FakeClient(positions=None)).get_positions()


def test_positions_empty_list_is_a_real_empty_book():
    assert _trading(_FakeClient(positions=[])).get_positions() == []


def test_sellable_none_is_unavailable_not_all_zero():
    """空表当真 0 ＝「每只都不能卖」⇒ 卖出腿一股不报。"""
    with pytest.raises(QueryUnavailable):
        _trading(_FakeClient(positions=None)).get_sellable()


def test_sellable_keeps_a_real_zero():
    """非空表是可信的：`CanUseVol` 含 0（T+1 当日买入），那个 0 是事实。"""
    c = _FakeClient(positions=[{"Code": "000761.SZ", "TotalVol": 100, "CanUseVol": 0}])
    assert _trading(c).get_sellable() == {"000761.SZ": "0.0"}


def test_orders_none_is_unavailable():
    with pytest.raises(QueryUnavailable):
        _trading(_FakeClient(orders=[None])).get_orders()


def test_order_status_follows_the_fill_not_the_status_code():
    """状态码含义随版本可能变，成交量是硬事实。"""
    c = _FakeClient(orders=[[_order_row(CjVol=100, Status=1)]])
    assert _trading(c).get_orders()[0]["status"] == "filled"


def test_partially_filled_after_a_cancel():
    c = _FakeClient(orders=[[_order_row(CjVol=30, BSFlag=-1)]])
    assert _trading(c).get_orders()[0]["status"] == "partially_filled"


def test_account_with_no_money_field_is_none():
    """实测有 `{"ErrorId": "0", "Value": []}` 这种形态，`if not a` 拦不住（dict 非空）。"""
    assert _trading(_FakeClient(asset={"ErrorId": "0", "Value": []})).get_account() is None


def test_account_maps_the_measured_keys():
    c = _FakeClient(asset={"Asset": 99282.77, "Cash": 21351.77, "Balance": 21351.77,
                           "TotalFreeze": 0, "TotalMargin": 0})
    acc = _trading(c).get_account()
    assert acc["total_equity"] == "99282.77" and acc["total_available"] == "21351.77"


# ---- 标的与快照 ----

def test_margin_target_is_three_state():
    c = _FakeClient(info={"BelongRZRQ": 0, "Name": "某股"})
    assert _trading(c).instrument("603230")["margin_target"] is False
    c2 = _FakeClient(info={"Name": "某股"})
    assert _trading(c2).instrument("603230")["margin_target"] is None
    assert _trading(_FakeClient(info=None)).instrument("603230") is None


def test_quote_reads_the_measured_fields():
    c = _FakeClient(snapshot={"Now": 4.83, "LastClose": 4.8, "Buyp": [4.82, 4.81],
                              "Sellp": 4.84})
    row = _trading(c).quotes(["000761"])[0]
    assert (row["last"], row["prev_close"], row["bid1"], row["ask1"]) == (
        "4.83", "4.8", "4.82", "4.84")


def test_quote_that_cannot_be_read_is_left_out():
    """判不了 != 没有这只票 ⇒ 不给一行全零的假快照。"""
    assert _trading(_FakeClient(snapshot=None)).quotes(["000761"]) == []


# ---- 读超时：查询与报单不是一回事 ----

class _ReadTimeout(Exception):
    """名字里带 ReadTimeout 就够了——判据看的是类名，不必把 requests 拉进用例。"""


def _timing_out(**kw):
    c = _FakeClient(**kw)

    def boom(*_a, **_k):
        try:
            raise _ReadTimeout("read timed out")
        except _ReadTimeout as e:
            raise TdxQuantConnectionError("MCP 调用失败") from e

    c.query_positions = boom
    c.query_orders = boom
    c.order_stock = boom
    return c


def test_a_query_read_timeout_is_only_unavailable():
    """查询超时报成 504 会让调用方为了一次快照没答话去熔断对账。"""
    t = _trading(_timing_out())
    with pytest.raises(DriverError) as got:
        t.get_positions()
    assert not isinstance(got.value, AckUnknown)


def test_an_order_read_timeout_is_state_unknown():
    """🔴 报单超时**不代表没报出去** ⇒ 必须与「通道不可用」分开。"""
    with pytest.raises(AckUnknown):
        _trading(_timing_out()).create_order(symbol="000761", side="buy", size=100,
                                             price=4.8)


def test_a_failing_confirm_query_does_not_hide_the_submitted_cancel():
    """撤单已经提交出去了，这时候往上抛「通道不可用」，调用方会以为什么都没发生。"""
    c = _FakeClient()

    def boom(*_a, **_k):
        raise TdxQuantConnectionError("确认查询打不通")

    c.query_orders = boom
    res = _trading(c).cancel_order(symbol="000761", order_id="88")
    assert res["canceled"] is False and "状态未定" in res["reason"]
    assert c.cancels == [("000761.SZ", "88")]

"""行情与静态数据：字段转换、口径、以及那几个只有真机才暴露出来的规矩。"""
from __future__ import annotations

import pytest

from cn_broker_api.config import Config, HealthConfig, ServerConfig, TdxQuantConfig
from cn_broker_api.drivers.capability import Capability
from cn_broker_api.drivers.driver_error import DriverError
from cn_broker_api.drivers.paper import PaperDriver
from cn_broker_api.drivers.tdxquant.market import TdxQuantMarketData
from cn_broker_api.drivers.tdxquant.mcp import TransportError
from cn_broker_api.http_app import create_app
from cn_broker_api.trade.query_unavailable import QueryUnavailable

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(driver, tmp_path):
    cfg = Config(server=ServerConfig(port=17710, state_dir=tmp_path),
                 health=HealthConfig(), tdxquant=TdxQuantConfig(), driver="paper")
    return create_app(cfg, driver, token=TOKEN).test_client()


@pytest.fixture()
def client(tmp_path):
    return _client(PaperDriver(), tmp_path)


class _FakeMcp:
    """假客户端。`answers` 按方法名给回执，`calls` 记下调用顺序。"""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def call(self, method, params, *, timeout=None):
        self.calls.append((method, params))
        got = self.answers.get(method)
        if isinstance(got, Exception):
            raise got
        if got is None:
            raise AssertionError(f"用例没给 {method} 的回执")
        return got


KLINES_OK = {"ErrorId": "0", "KlineTotal": {"000761.SZ": 2}, "has_more": False,
             "Value": {"000761.SZ": {
                 "Date": ["20260820", "20260821"], "Time": ["0", "0"],
                 "Open": ["2.30", "2.35"], "High": ["2.36", "2.35"],
                 "Low": ["2.28", "2.29"], "Close": ["2.35", "2.33"],
                 "Volume": ["16521600.00", "15310400.00"],
                 "Amount": ["3842.38", "3553.91"]}}}

SNAP_OK = {"ErrorId": "0", "Now": "2.33", "LastClose": "2.35", "Open": "2.35",
           "Max": "2.35", "Min": "2.29", "Average": "2.32", "Volume": "153104",
           "Amount": "3553.91", "NowVol": "679", "ItemNum": "1781",
           "Inside": "75163", "Outside": "77941",
           "Buyp": ["2.33", "2.32", "0.00", "0.00", "0.00"],
           "Buyv": ["274", "500", "0", "0", "0"],
           "Sellp": ["2.34", "2.35", "0.00", "0.00", "0.00"],
           "Sellv": ["3918", "120", "0", "0", "0"]}


# ---- K 线 ----

def test_columns_become_one_row_per_bar():
    m = TdxQuantMarketData(_FakeMcp(get_market_data=KLINES_OK))
    got = m.klines(["000761"], period="1d", count=2)
    assert [r["datetime"] for r in got["rows"]] == ["20260820", "20260821"]
    assert got["rows"][1]["close"] == "2.33"


def test_units_ride_along_with_the_response():
    """⚠️ 同一个客户端里 K 线的成交量是**股**、`/v1/prices` 那边是**手**。
    让调用方去记这件事，就是等着有人算错一百倍。"""
    m = TdxQuantMarketData(_FakeMcp(get_market_data=KLINES_OK))
    assert m.klines(["000761"], count=2)["units"] == {"volume": "shares",
                                                     "amount": "wan_yuan"}


def test_minute_bars_get_a_clock_in_the_stamp():
    res = {"ErrorId": "0", "Value": {"000761.SZ": {
        "Date": ["20260821"], "Time": ["145900"], "Open": ["2.34"], "High": ["2.34"],
        "Low": ["2.34"], "Close": ["2.34"], "Volume": ["0.00"], "Amount": ["0.00"]}}}
    m = TdxQuantMarketData(_FakeMcp(get_market_data=res))
    assert m.klines(["000761"], period="1m", count=1)["rows"][0]["datetime"] == \
        "20260821145900"


def test_refresh_happens_before_the_request():
    """⚠️ 实测：分钟线不刷缓存就是空的（`KlineTotal` 有数、行是空的）。
    顺序反了等于没刷。"""
    fake = _FakeMcp(get_market_data=KLINES_OK,
                    refresh_kline={"ErrorId": "0", "Msg": "ok"})
    TdxQuantMarketData(fake).klines(["000761"], period="1m", count=2, refresh=True)
    assert [c[0] for c in fake.calls] == ["refresh_kline", "get_market_data"]


def test_no_refresh_unless_asked():
    fake = _FakeMcp(get_market_data=KLINES_OK)
    TdxQuantMarketData(fake).klines(["000761"], count=2)
    assert [c[0] for c in fake.calls] == ["get_market_data"]


def test_a_bad_period_is_refused_before_the_call():
    """写歪了厂商回的是 `error: -5`，不如在发出前就说清楚。"""
    fake = _FakeMcp()
    with pytest.raises(ValueError, match="period"):
        TdxQuantMarketData(fake).klines(["000761"], period="3m")
    assert fake.calls == []


def test_kline_error_is_unavailable_not_empty():
    m = TdxQuantMarketData(_FakeMcp(get_market_data={"ErrorId": "2", "Error": "坏了"}))
    with pytest.raises(QueryUnavailable):
        m.klines(["000761"], count=2)


def test_transport_failure_is_a_driver_error():
    m = TdxQuantMarketData(_FakeMcp(get_market_data=TransportError("连不上")))
    with pytest.raises(DriverError):
        m.klines(["000761"], count=2)


# ---- 快照与五档 ----

def test_depth_carries_the_five_levels_with_sizes():
    """⭐ 封板时买一那档的挂单量就是封单量，这是要 depth 的主要理由。"""
    m = TdxQuantMarketData(_FakeMcp(get_market_snapshot=SNAP_OK))
    row = m.quotes(["000761"], depth=True)[0]
    assert row["bids"][0] == {"price": "2.33", "size": "274"}
    assert row["asks"][1] == {"price": "2.35", "size": "120"}
    assert row["inside"] == "75163" and row["last_size"] == "679"


def test_without_depth_the_shape_stays_the_old_one():
    m = TdxQuantMarketData(_FakeMcp(get_market_snapshot=SNAP_OK))
    row = m.quotes(["000761"])[0]
    assert row["bid1"] == "2.33" and "bids" not in row


def test_a_snapshot_that_cannot_be_read_is_left_out():
    """判不了 != 没有这只票 ⇒ 不给一行全零的假快照。"""
    m = TdxQuantMarketData(_FakeMcp(get_market_snapshot={"ErrorId": "2"}))
    assert m.quotes(["000761"]) == []


# ---- 批量报价 ----

def test_prices_are_one_call_for_many_codes():
    fake = _FakeMcp(get_pricevol={"ErrorId": "0", "Value": {
        "000761.SZ": {"Now": "2.33", "LastClose": "2.35", "Volume": "153104"},
        "600519.SH": {"Now": "1272.83", "LastClose": "1291.50", "Volume": "33472"}}})
    rows = TdxQuantMarketData(fake).prices(["000761", "600519"])
    assert len(fake.calls) == 1 and len(rows) == 2
    assert rows[0]["volume"] == "153104"


# ---- 封板状态 ----

def test_limit_status_keeps_vendor_keys_and_drops_the_broken_code():
    """⚠️ 实测厂商的 `Code` 字段是截断的（`".SZ"`）⇒ 只认外层那个键。
    其余键名原样转发：猜错一个键会把「没封板」读成「封住了」。"""
    fake = _FakeMcp(get_zdt_data={"ErrorId": "0", "Value": {"000761.SZ": {
        "Code": ".SZ", "ZDTStatusNow": "1", "VolZT": "12345.00",
        "FirstTimeZT": "093015", "OpenTimesZT": "2"}}})
    row = TdxQuantMarketData(fake).limit_status(["000761"])[0]
    assert row["symbol"] == "000761.SZ" and "Code" not in row
    assert row["VolZT"] == "12345.00" and row["OpenTimesZT"] == "2"


# ---- 除权除息 ----

DIVID = {"ErrorId": "0",
         "Date": ["20190710", "20210719", "20220616"],
         "Type": ["1", "1", "1"],
         "Value": [["0.20", "0.00", "0.00", "0.00"],
                   ["0.10", "0.00", "0.00", "0.00"],
                   ["0.30", "0.00", "0.00", "0.00"]]}


def test_dividend_dates_are_filtered_on_our_side():
    """⚠️ 厂商源码里写着那两个时间参数在 C 接口上没有实际作用、返回的是全部权息数据。
    指望它过滤，就会把 2019 年那条当成"区间内的"。"""
    m = TdxQuantMarketData(_FakeMcp(get_divid_factors=DIVID))
    rows = m.dividends("000761", start="20210101", end="20211231")
    assert [r["date"] for r in rows] == ["20210719"]


def test_dividend_columns_follow_the_vendor_order():
    m = TdxQuantMarketData(_FakeMcp(get_divid_factors=DIVID))
    row = m.dividends("000761")[0]
    assert (row["bonus"], row["allot_price"], row["share_bonus"], row["allotment"]) == \
        ("0.20", "0.00", "0.00", "0.00")


# ---- 端点 ----

def test_klines_endpoint_shape(client):
    body = client.get("/v1/klines?codes=000761&period=1d&count=5", headers=AUTH).get_json()
    assert body["known"] is True and body["period"] == "1d"
    assert body["units"] == {"volume": "shares", "amount": "wan_yuan"}


def test_klines_need_codes(client):
    assert client.get("/v1/klines", headers=AUTH).status_code == 400


def test_klines_reject_a_silly_count(client):
    assert client.get("/v1/klines?codes=000761&count=99999",
                      headers=AUTH).status_code == 400


def test_prices_say_the_unit_is_lots(client):
    body = client.get("/v1/prices?codes=000761", headers=AUTH).get_json()
    assert body["units"] == {"volume": "lots"}


def test_limit_status_and_dividends_are_three_state(client):
    assert client.get("/v1/limit-status?codes=000761", headers=AUTH).get_json() == \
        {"known": True, "rows": []}
    assert client.get("/v1/dividends/000761", headers=AUTH).get_json() == \
        {"known": True, "rows": []}


def test_a_driver_without_market_data_gets_501(tmp_path):
    """⭐ ctypes 通道上这一族没实现 ⇒ 能力不声明，端点明确 501，
    而不是让调用方撞一个看不懂的错。"""
    class _NoMarket(PaperDriver):
        def capabilities(self):
            return [c for c in super().capabilities() if c != Capability.MARKET_DATA]

    r = _client(_NoMarket(), tmp_path).get("/v1/klines?codes=000761", headers=AUTH)
    assert r.status_code == 501 and r.get_json()["error"] == "capability_missing"


def test_diag_endpoints_need_the_desktop_capability(client):
    """纸面驱动没有窗口可收、也没有位图可抓 ⇒ 501，不是假装成功。"""
    assert client.post("/v1/session/minimize", headers=AUTH).status_code == 501
    assert client.get("/v1/diag/screenshot", headers=AUTH).status_code == 501

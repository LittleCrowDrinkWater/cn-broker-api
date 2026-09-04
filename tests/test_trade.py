"""交易与行情端点：线上表达与错误翻译。全部走纸面驱动或假件 ⇒ 任何平台都跑得过。"""
from __future__ import annotations

import pytest

from cn_broker_api.config import Config, HealthConfig, ServerConfig, TdxQuantConfig
from cn_broker_api.drivers.driver_error import DriverError
from cn_broker_api.drivers.paper import PaperDriver
from cn_broker_api.http_app import create_app
from cn_broker_api.queue_timeout import QueueTimeout
from cn_broker_api.trade.ack_unknown import AckUnknown
from cn_broker_api.trade.order_pending_confirm import OrderPendingConfirm
from cn_broker_api.trade.order_rejected import OrderRejected
from cn_broker_api.trade.query_unavailable import QueryUnavailable

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORDER = {"symbol": "000761", "side": "buy", "size": 100, "price": 4.83}


def _app(driver, tmp_path):
    cfg = Config(server=ServerConfig(port=17710, state_dir=tmp_path),
                 health=HealthConfig(), tdxquant=TdxQuantConfig(), driver="paper")
    return create_app(cfg, driver, token=TOKEN).test_client()


@pytest.fixture()
def client(tmp_path):
    return _app(PaperDriver(), tmp_path)


class _Boom:
    """每个动词都抛同一个异常的交易对象。"""

    def __init__(self, exc):
        self.exc = exc

    def __getattr__(self, _name):
        def raise_it(**_kw):
            raise self.exc
        return raise_it


class _BoomDriver(PaperDriver):
    def __init__(self, exc):
        super().__init__()
        self._exc = exc

    def trading(self, *, account: str = "", account_type: str = "STOCK"):
        return _Boom(self._exc)


def _boom(tmp_path, exc):
    return _app(_BoomDriver(exc), tmp_path)


# ---- 鉴权：下单端点一样要 token ----

def test_orders_need_a_token(client):
    assert client.post("/v1/orders", json=ORDER).status_code == 401


# ---- 报单 ----

def test_create_order_is_201_with_a_row(client):
    r = client.post("/v1/orders", json=ORDER, headers=AUTH)
    assert r.status_code == 201
    row = r.get_json()["order"]
    assert row["symbol"] == "000761.SZ"          # 归一在入口做掉，裸码不往下漏
    assert row["status"] == "live"
    assert row["side"] == "buy"


def test_numbers_go_over_the_wire_as_strings(client):
    """调用方那侧是 Decimal 账本：`Decimal(str)` 精确，`Decimal(float)` 会把误差烘进去。"""
    row = client.post("/v1/orders", json=ORDER, headers=AUTH).get_json()["order"]
    assert isinstance(row["size"], str) and isinstance(row["price"], str)


def test_missing_symbol_is_400(client):
    r = client.post("/v1/orders", json={**ORDER, "symbol": ""}, headers=AUTH)
    assert r.status_code == 400


def test_bad_size_is_400_not_500(client):
    r = client.post("/v1/orders", json={**ORDER, "size": "一百股"}, headers=AUTH)
    assert r.status_code == 400


@pytest.mark.parametrize("bad", [
    {"side": "long"}, {"side": ""}, {"size": 0}, {"size": -100}, {"price": 0}, {"price": -1},
])
def test_illegal_order_fields_are_400_and_never_reach_the_driver(client, bad):
    """字段不合法＝调用方的 bug ⇒ 400，且**不占账户串行槽**（压根不排队）。

    ⚠️ 别让它变成 503：503 的意思是"到客户端那条通道不可用"，排查会从错的一头开始。
    """
    r = client.post("/v1/orders", json={**ORDER, **bad}, headers=AUTH)
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad_request"
    assert client.get("/v1/orders", headers=AUTH).get_json()["rows"] == []


def test_unknown_credit_kind_is_400(client):
    """认不出的 credit_kind **不能静默忽略**：忽略的表现是"我以为报的是融资买入，
    其实报的是普通买入"，而两者的负债完全不同。"""
    r = client.post("/v1/orders", json={**ORDER, "credit_kind": "rong_zi"}, headers=AUTH)
    assert r.status_code == 400


def test_credit_order_on_a_driver_without_that_capability_is_501(client):
    """能力不够就明确失败，**不静默降级**——降级的表现是「那天等于没调仓」。"""
    r = client.post("/v1/orders", json={**ORDER, "credit_kind": "fin_buy"}, headers=AUTH)
    assert r.status_code == 501
    assert r.get_json()["error"] == "capability_missing"


# ---- 撤单 ----

def test_cancel_needs_a_symbol(client):
    r = client.delete("/v1/orders/paper-1", headers=AUTH)
    assert r.status_code == 400


def test_cancel_reports_the_fact(client):
    client.post("/v1/orders", json=ORDER, headers=AUTH)
    r = client.delete("/v1/orders/paper-1?symbol=000761.SZ", headers=AUTH)
    assert r.status_code == 200
    body = r.get_json()
    assert body["canceled"] is True and body["order"]["status"] == "canceled"
    # `outcome` 是撤单这条路上唯一分得开「已成交」与「超时没等到」的一位，必须过得了网
    assert body["outcome"] == "canceled"


# ---- 查询三态 ----

def test_empty_positions_are_known_empty(client):
    """空表是**真的空仓**，与"判不了"必须分得开。"""
    body = client.get("/v1/positions", headers=AUTH).get_json()
    assert body == {"known": True, "rows": []}


def test_unavailable_query_is_known_false_and_carries_no_rows(tmp_path):
    """🔴 这条是对账那条链的守卫：`known` 位丢掉的症状不是报错，是某天早上把持仓账本
    整表删了。所以既要有 `known: false`，**也不能给出 rows**——给了空表下游就会拿它当事实。"""
    c = _boom(tmp_path, QueryUnavailable("持仓查询返回空内容"))
    r = c.get("/v1/positions", headers=AUTH)
    assert r.status_code == 200
    body = r.get_json()
    assert body["known"] is False and "rows" not in body
    assert "空内容" in body["reason"]


def test_sellable_unavailable_is_not_an_empty_dict(tmp_path):
    """可卖量空表当真 0 的后果：卖出腿一股不报（负债过夜 / 那天等于没调仓）。"""
    c = _boom(tmp_path, QueryUnavailable("可卖量判不了"))
    body = c.get("/v1/positions/sellable", headers=AUTH).get_json()
    assert body["known"] is False and "volumes" not in body


def test_account_that_cannot_be_read_is_not_a_zero_account(tmp_path):
    """取不到与权益是 0 是两件事：后者会被当成真实数字（探活显示 0、预算算出 0 手）。"""
    class _NoAccount(PaperDriver):
        def trading(self, *, account="", account_type="STOCK"):
            class _T:
                def get_account(self):
                    return None
            return _T()

    body = _app(_NoAccount(), tmp_path).get("/v1/account", headers=AUTH).get_json()
    assert body["known"] is False and "account" not in body


# ---- 委托中间态与拒单 ----

def test_pending_confirm_is_202_not_409(tmp_path):
    """🔴 202 ＝结果还没定。2026-08-19 把它当拒单处置，柜台上已成交 8600 股而账本记
    failed ⇒ 卖券还款只扫 bought，那笔融资买入差点负债过夜。"""
    c = _boom(tmp_path, OrderPendingConfirm("委托待客户端确认：已发送信号至客户端"))
    r = c.post("/v1/orders", json=ORDER, headers=AUTH)
    assert r.status_code == 202
    assert r.get_json()["pending_confirm"] is True


def test_rejected_is_409(tmp_path):
    c = _boom(tmp_path, OrderRejected("下单被拒：非融资品标的买入[603230]"))
    r = c.post("/v1/orders", json=ORDER, headers=AUTH)
    assert r.status_code == 409
    assert "非融资品标的" in r.get_json()["broker_message"]


def test_channel_unavailable_is_503(tmp_path):
    c = _boom(tmp_path, DriverError("连不上通达信量化"))
    assert c.post("/v1/orders", json=ORDER, headers=AUTH).status_code == 503


@pytest.mark.parametrize("exc", [AckUnknown("调用超时，状态未知"),
                                 QueueTimeout("排队 120 秒仍未轮到")])
def test_unknown_state_is_504_not_503(tmp_path, exc):
    """504 与 503 的处置完全相反：前者必须回柜台重新观测，后者是压根没发出去。"""
    c = _boom(tmp_path, exc)
    r = c.post("/v1/orders", json=ORDER, headers=AUTH)
    assert r.status_code == 504 and r.get_json()["error"] == "ack_timeout"


# ---- 行情与标的 ----

def test_quotes_need_codes(client):
    assert client.get("/v1/quotes", headers=AUTH).status_code == 400


def test_quotes_are_capped(client):
    """厂商没有批量接口，N 个代码就是 N 次调用，而它们与报单共用同一条串行连接。"""
    codes = ",".join(f"{i:06d}" for i in range(60))
    assert client.get(f"/v1/quotes?codes={codes}", headers=AUTH).status_code == 400


def test_quotes_return_rows(client):
    body = client.get("/v1/quotes?codes=000001,600519", headers=AUTH).get_json()
    assert [r["symbol"] for r in body["rows"]] == ["000001.SZ", "600519.SH"]


def test_instrument_margin_target_may_be_null(client):
    """判不了就是 null。谎报 True 会让调用方拿一份假的两融名单去下融资单。"""
    body = client.get("/v1/instruments/000761", headers=AUTH).get_json()
    assert body["instrument"]["margin_target"] is None


# ---- 契约版本 ----

def test_contract_says_four_since_side_can_be_unknown(client):
    """字段一变版本就得动，否则调用方校版本这件事就白做了。

    v2 ＝交易端点上线，v3 ＝委托行多了 `order_time`，v4 ＝委托行的 `side` 可以是 null
    （已撤单柜台不给方向）。这条用例的作用是**逼着改版本号**：两侧靠这个整数硬校验，
    忘了升就会在调用方那侧凌晨三点静默读到一个恒为 null 的字段。
    ⭐ v4 这次尤其要升：老调用方对 null 的处置是 `or "buy"`，**静默折成买入**——
    那不是少一个字段，是记反一笔账。
    """
    assert client.get("/v1/meta", headers=AUTH).get_json()["contract"] == 4

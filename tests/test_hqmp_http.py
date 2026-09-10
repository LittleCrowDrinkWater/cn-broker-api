"""直接 HQMP 适配层经正式 HTTP 路由的契约测试。"""
from __future__ import annotations

from cn_broker_api.config import Config, HealthConfig, ServerConfig, TdxQuantConfig
from cn_broker_api.drivers.capability import Capability
from cn_broker_api.drivers.paper import PaperDriver
from cn_broker_api.drivers.tdxquant.hqmp_direct_trading import HqmpDirectTrading
from cn_broker_api.http_app import create_app


class _DirectSession:
    enable_trade = True

    def __init__(self):
        self.placed = []

    def require_account(self, _account, *, timeout):
        return None

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return {"order_id": "private-order", "message": ""}


class _DirectDriver(PaperDriver):
    name = "tdxquant"

    def __init__(self):
        super().__init__()
        self.session = _DirectSession()
        self.direct = HqmpDirectTrading(
            self.session,
            account="",
            account_type="CREDIT",
            instrument_of=lambda _code: None,
            max_order_size=100,
            max_order_notional=2000.0,
        )

    def trading(self, *, account: str = "", account_type: str = "STOCK"):
        return self.direct

    def capabilities(self) -> list[str]:
        return [
            Capability.CREDIT_ORDER,
            Capability.CANCEL,
            Capability.SELLABLE_VOLUME,
        ]


def _client(tmp_path):
    cfg = Config(
        server=ServerConfig(state_dir=tmp_path),
        health=HealthConfig(),
        tdxquant=TdxQuantConfig(),
        driver="paper",
    )
    driver = _DirectDriver()
    return driver, create_app(cfg, driver, token="token").test_client()


def test_request_security_name_cannot_override_the_symbol_identity(tmp_path):
    driver, client = _client(tmp_path)

    response = client.post(
        "/v1/orders",
        headers={"Authorization": "Bearer token"},
        json={
            "account_type": "CREDIT",
            "symbol": "000001.SZ",
            "security_name": "错误的名称",
            "side": "buy",
            "size": 100,
            "price": 10.61,
            "credit_kind": "collateral_buy",
        },
    )

    assert response.status_code == 201
    assert driver.session.placed[0]["symbol"] == "000001.SZ"
    assert driver.session.placed[0]["security_name"] == ""


def test_missing_security_name_is_accepted_and_symbol_remains_authoritative(tmp_path):
    driver, client = _client(tmp_path)

    response = client.post(
        "/v1/orders",
        headers={"Authorization": "Bearer token"},
        json={
            "account_type": "CREDIT",
            "symbol": "000001.SZ",
            "side": "buy",
            "size": 100,
            "price": 10.61,
        },
    )

    assert response.status_code == 201
    assert driver.session.placed[0]["symbol"] == "000001.SZ"
    assert driver.session.placed[0]["security_name"] == ""

"""直接 HQMP 会话到统一交易端口的适配层。"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

from cn_broker_api.drivers.driver_error import DriverError
from cn_broker_api.symbols import market_of, symbol_key, to_tq_code
from cn_broker_api.trade.ack_unknown import AckUnknown
from cn_broker_api.trade.credit_kind import CREDIT_KIND_SIDE, CreditOrderKind
from cn_broker_api.trade.order_rejected import OrderRejected
from cn_broker_api.trade.query_unavailable import QueryUnavailable
from cn_broker_api.trade.wire import account_row, order_row, position_row

from .hqmp_direct import DIRECT_MONEY_FIELDS, HqmpDirectSession, _row_value


class _MarketPort(Protocol):
    def quotes(self, codes: Sequence[str], *, depth: bool = False) -> List[Dict[str, Any]]:
        ...

    def instrument(self, code: str) -> Optional[Dict[str, Any]]:
        ...


class HqmpDirectTrading:
    """将已启动的直接 HQMP 会话映射成 ``Trading`` 契约。

    委托身份只使用已规范化的证券代码和市场。``instrument_of`` 只用于尽力
    补充 TC 报文中的展示名称；查不到时传空，不因此拒绝已通过风控的委托。
    行情不属于直接 HQMP 已验证能力；如需 ``quotes``/``instrument`` 端点，必须
    注入独立的 ``market_port``。
    """

    def __init__(
        self,
        session: HqmpDirectSession,
        *,
        account: str = "",
        account_type: str,
        instrument_of: Callable[[str], Optional[Dict[str, Any]]],
        market_port: Optional[_MarketPort] = None,
        call_timeout: float = 20.0,
        cancel_visibility_timeout: float = 10.0,
        cancel_confirm_timeout: float = 10.0,
        cancel_confirm_interval: float = 0.2,
        max_order_size: int = 100,
        max_order_notional: float = 2000.0,
    ) -> None:
        self._session = session
        self._account = str(account).strip()
        self._account_type = str(account_type).strip().upper()
        self._instrument_of = instrument_of
        self._market_port = market_port
        self._call_timeout = call_timeout
        self._cancel_visibility_timeout = cancel_visibility_timeout
        self._cancel_confirm_timeout = cancel_confirm_timeout
        self._cancel_confirm_interval = cancel_confirm_interval
        self._max_order_size = int(max_order_size)
        self._max_order_notional = float(max_order_notional)
        if self._max_order_size <= 0 or self._max_order_notional <= 0:
            raise ValueError("直接 HQMP 单笔数量和金额上限必须为正数")

    @property
    def is_credit(self) -> bool:
        return self._account_type == "CREDIT"

    def _require_account(self) -> None:
        try:
            self._session.require_account(self._account, timeout=self._call_timeout)
        except (QueryUnavailable, ValueError):
            raise
        except (ConnectionError, OSError, RuntimeError, TimeoutError) as exc:
            raise DriverError(f"直接 HQMP 账户核对不可用：{exc}") from exc

    def create_order(
        self,
        *,
        symbol: str,
        side: str,
        size: int,
        price: Optional[float] = None,
        order_type: str = "limit",
        client_order_id: Optional[str] = None,
        credit_kind: Optional[CreditOrderKind] = None,
        notify: Optional[int] = None,
    ) -> Dict[str, Any]:
        """发限价委托；本通道直接进柜台，``notify`` 没有可对应的人工确认层。"""
        if not self._session.enable_trade:
            raise DriverError("直接 HQMP 交易闸未打开，拒绝报单")
        if order_type != "limit":
            raise ValueError(f"直接 HQMP 只支持限价单，收到 {order_type!r}")
        if price is None or not math.isfinite(float(price)) or float(price) <= 0:
            raise ValueError("限价单要给正的 price")
        if int(size) <= 0 or int(size) != size:
            raise ValueError(f"委托数量要是正整数，收到 {size!r}")
        if int(size) > self._max_order_size:
            raise ValueError(
                f"委托数量 {int(size)} 超过直接 HQMP 单笔上限 {self._max_order_size}"
            )
        notional = int(size) * float(price)
        if notional > self._max_order_notional:
            raise ValueError(
                f"委托金额 {notional:.2f} 超过直接 HQMP 单笔上限 "
                f"{self._max_order_notional:.2f}"
            )
        side = str(side).strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError(f"side 只能是 buy / sell，收到 {side!r}")
        if notify is not None:
            raise ValueError("直接 HQMP 不经过页面确认层，不支持 notify")
        if credit_kind is not None:
            if not self.is_credit:
                raise ValueError(
                    f"账户类别 {self._account_type} 不能下信用委托 {credit_kind.name}"
                )
            expected_side = CREDIT_KIND_SIDE[credit_kind]
            if side != expected_side:
                raise ValueError(
                    f"信用委托 {credit_kind.name} 的方向必须是 {expected_side}，收到 {side}"
                )

        code = to_tq_code(symbol)
        self._require_account()
        instrument = self._instrument_of(code)
        name = str((instrument or {}).get("name") or "").strip()
        try:
            result = self._session.place_order(
                symbol=code,
                security_name=name,
                side=side,
                size=int(size),
                price=float(price),
                credit_kind=credit_kind,
                timeout=self._call_timeout,
            )
        except (AckUnknown, OrderRejected, QueryUnavailable, ValueError):
            raise
        except (ConnectionError, OSError, RuntimeError, TimeoutError) as exc:
            raise DriverError(f"直接 HQMP 报单通道不可用：{exc}") from exc
        return order_row(
            order_id=result["order_id"],
            client_order_id=client_order_id,
            symbol=code,
            side=side,
            status="live",
            size=size,
            price=price,
        )

    def cancel_order(self, *, symbol: str, order_id: str) -> Dict[str, Any]:
        if not self._session.enable_trade:
            raise DriverError("直接 HQMP 交易闸未打开，拒绝撤单")
        try:
            self._require_account()
            return self._session.cancel_order_and_wait(
                symbol=to_tq_code(symbol),
                order_id=order_id,
                visibility_timeout=self._cancel_visibility_timeout,
                settle_timeout=self._cancel_confirm_timeout,
                interval=self._cancel_confirm_interval,
                call_timeout=self._call_timeout,
            )
        except (AckUnknown, OrderRejected, QueryUnavailable, ValueError):
            raise
        except (ConnectionError, OSError, RuntimeError, TimeoutError) as exc:
            raise DriverError(f"直接 HQMP 撤单通道不可用：{exc}") from exc

    def get_order(self, *, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        try:
            self._require_account()
            row = self._session.query_order(order_id=order_id, timeout=self._call_timeout)
        except QueryUnavailable:
            raise
        except (ConnectionError, OSError, RuntimeError, TimeoutError) as exc:
            raise DriverError(f"直接 HQMP 委托查询不可用：{exc}") from exc
        return None if row is None else self._session._direct_order_row(row)

    def get_orders(self) -> List[Dict[str, Any]]:
        try:
            self._require_account()
            return [self._session._direct_order_row(row)
                    for row in self._session.query_orders(timeout=self._call_timeout)]
        except QueryUnavailable:
            raise
        except (ConnectionError, OSError, RuntimeError, TimeoutError) as exc:
            raise DriverError(f"直接 HQMP 当日委托查询不可用：{exc}") from exc

    @staticmethod
    def _symbol_of(row: Dict[str, Any]) -> str:
        code = str(_row_value(row, "zqdm", "Code", "StockCode") or "").strip()
        if len(symbol_key(code)) != 6 or not symbol_key(code).isdigit():
            raise QueryUnavailable("直接 HQMP 持仓行缺少有效的 6 位证券代码")
        setcode = str(_row_value(row, "setcode") or "").strip()
        market = "SH" if setcode == "1" else ("SZ" if setcode == "0" else market_of(code))
        return f"{symbol_key(code)}.{market}"

    def get_positions(self) -> List[Dict[str, Any]]:
        try:
            self._require_account()
            raw = self._session.query_positions(timeout=self._call_timeout)
        except QueryUnavailable:
            raise
        except (ConnectionError, OSError, RuntimeError, TimeoutError) as exc:
            raise DriverError(f"直接 HQMP 持仓查询不可用：{exc}") from exc
        rows = []
        for item in raw:
            size = _row_value(item, "zqsl", "TotalVol", "Volume")
            try:
                if float(size or 0) <= 0:
                    continue
            except (TypeError, ValueError):
                raise QueryUnavailable("直接 HQMP 持仓数量无法解析") from None
            rows.append(position_row(
                symbol=self._symbol_of(item),
                size=size,
                avg_price=_row_value(item, "cbj", "CostPrice", "AvgPrice"),
                mark_price=_row_value(item, "zxj", "MarketPrice", "NowPrice"),
                unrealized_pnl=_row_value(item, "fdyk", "ProfitLoss") or 0,
                sellable=_row_value(item, "kmsl", "CanUseVol", "KyVol"),
            ))
        return rows

    def get_sellable(self) -> Dict[str, Optional[str]]:
        return {row["symbol"]: row["sellable"] for row in self.get_positions()}

    def get_account(self) -> Optional[Dict[str, Any]]:
        try:
            self._require_account()
            rows = self._session.query_assets(timeout=self._call_timeout)
        except QueryUnavailable:
            raise
        except (ConnectionError, OSError, RuntimeError, TimeoutError) as exc:
            raise DriverError(f"直接 HQMP 资产查询不可用：{exc}") from exc
        candidates = [row for row in rows if DIRECT_MONEY_FIELDS.intersection(row)]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise QueryUnavailable("直接 HQMP 资产查询返回多条资金行，拒绝猜测")
        item = candidates[0]
        return account_row(
            total_equity=_row_value(item, "zican"),
            total_available=_row_value(item, "keyong"),
            total_unrealized_pnl=_row_value(item, "yk") or 0,
            cash_balance=_row_value(item, "yu") or 0,
            frozen=0,
        )

    def quotes(self, codes: Sequence[str], *, depth: bool = False) -> List[Dict[str, Any]]:
        if self._market_port is None:
            raise DriverError("直接 HQMP 尚未验证行情能力，且未注入独立行情通道")
        return self._market_port.quotes(codes, depth=depth)

    def instrument(self, code: str) -> Optional[Dict[str, Any]]:
        if self._market_port is not None:
            return self._market_port.instrument(code)
        return self._instrument_of(to_tq_code(code))

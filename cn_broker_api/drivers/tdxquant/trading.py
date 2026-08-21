"""桌面客户端上的交易与查询。判据全部从母项目的网关整段搬来，勿重写。"""
from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Sequence

from cn_broker_api.drivers.driver_error import DriverError
from cn_broker_api.drivers.tdxquant.client import TdxQuantClient, TdxQuantConnectionError
from cn_broker_api.drivers.tdxquant.fields import (f, is_pending_confirm, order_side,
                                                   order_status, s)
from cn_broker_api.symbols import to_tq_code
from cn_broker_api.trade.ack_unknown import AckUnknown
from cn_broker_api.trade.credit_kind import CREDIT_KIND_SIDE, CreditOrderKind
from cn_broker_api.trade.order_pending_confirm import OrderPendingConfirm
from cn_broker_api.trade.order_rejected import OrderRejected
from cn_broker_api.trade.query_unavailable import QueryUnavailable
from cn_broker_api.trade.wire import (CANCELED, FILLED, LIVE, account_row, num,
                                      order_row, position_row, quote_row)

logger = logging.getLogger(__name__)


def _vendor_errors(*, mutating: bool = False) -> Callable:
    """把厂商那侧的传输层失败翻译成本服务的错误分类。

    🔴 **只有会改变状态的调用（报单/撤单）才把读超时算成 `AckUnknown`（504）**：那种情况下
    请求已经发出去了、只是没等到答复，而报单超时不代表没报出去。查询超时没有这一层——
    把它也报成 504 会让调用方为了一次快照没答话去熔断对账。
    """
    def decorate(method: Callable) -> Callable:
        @wraps(method)
        def inner(self, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            except TdxQuantConnectionError as e:
                cause = type(e.__cause__).__name__ if e.__cause__ else ""
                if mutating and "ReadTimeout" in cause:
                    raise AckUnknown(f"调用超时，状态未知：{e}") from e
                raise DriverError(str(e)) from e
        return inner
    return decorate


#: 撤单确认循环里「本轮查询判不了」的哨兵——它既不是委托行也不是 None（None＝不在簿＝已撤）。
_QUERY_BLIP = object()

#: 资产 dict 里的资金字段（有其一才算真的取到了资产）。
_MONEY_KEYS = ("Asset", "Balance", "Cash")


class TdxQuantTrading:
    """一个账户上的交易动词。实例由驱动按 (账号, 类别) 给出，底下是进程级那条共享连接。"""

    def __init__(self, client: TdxQuantClient, *, cancel_timeout: float = 5.0,
                 cancel_interval: float = 1.0) -> None:
        self._client = client
        self._cancel_timeout = cancel_timeout
        self._cancel_interval = cancel_interval

    @property
    def is_credit(self) -> bool:
        return self._client.account_type == "CREDIT"

    def connect(self) -> None:
        """接上共享连接。**首败重试一次**——账户切换失败的主因是客户端侧状态没接稳，
        而 client 的失败路径已把全局状态压平，紧接着的第二次就是一次干净的新连接。

        Raises:
            DriverError: 重试之后仍然连不上。
        """
        last = None
        for attempt in (1, 2):
            try:
                self._client.connect()
                return
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt == 1:
                    logger.warning("[tdx] 连接失败（第 1 次，底层已重置，立即重试）: %s", e)
        raise DriverError(f"连不上通达信量化（重试一次后放弃）：{last}")

    # ---- 下单 ----
    @_vendor_errors(mutating=True)
    def create_order(self, *, symbol: str, side: str, size: int,
                     price: Optional[float] = None, order_type: str = "limit",
                     client_order_id: Optional[str] = None,
                     credit_kind: Optional[CreditOrderKind] = None,
                     notify: Optional[int] = None) -> Dict[str, Any]:
        """报一笔限价单。

        `credit_kind` 给了就按那个信用委托类型报；不给一律报普通买卖——**本服务不持有
        「默认按融资还是按担保品」这种模式**，那会让同一个请求在两台机器上产生不同的负债。

        `notify` 只为受控实验留着：实测传 0 时实盘账户照样回「待用户确认」，
        这个开关关不掉人工确认。

        Raises:
            OrderPendingConfirm: 推给客户端等人确认。
            OrderRejected: 定性拒单。
            DriverError: 参数不合法或通道不可用。
        """
        if order_type != "limit":
            raise DriverError(f"这条通道只支持限价单，收到 {order_type!r}")
        if price is None or float(price) <= 0:
            raise DriverError("限价单要给正的 price")
        if int(size) <= 0:
            raise DriverError(f"委托数量要是正整数，收到 {size!r}")
        side = str(side).strip().lower()
        if side not in ("buy", "sell"):
            raise DriverError(f"side 只能是 buy / sell，收到 {side!r}")

        self.connect()
        tqc = self._client.tqconst
        code = to_tq_code(symbol)
        kind_no = (self._credit_order_type(tqc, credit_kind, side) if credit_kind
                   else (tqc.STOCK_BUY if side == "buy" else tqc.STOCK_SELL))
        res = self._client.order_stock(code, kind_no, int(size), tqc.PRICE_MY, float(price),
                                       **({} if notify is None else {"notify": int(notify)}))
        msg = str(res.get("Msg") if isinstance(res, dict) else res)
        if is_pending_confirm(res):
            # 此刻没有委托编号（要等确认之后柜台才给）⇒ 调用方只能过一会儿重新观测柜台。
            raise OrderPendingConfirm(f"委托待客户端确认：{msg}", broker_message=msg)
        if not (isinstance(res, dict) and str(res.get("ErrorId")) == "0" and res.get("Wtbh")):
            raise OrderRejected(f"下单被拒：{msg}", broker_message=msg)
        return order_row(order_id=str(res["Wtbh"]), client_order_id=client_order_id,
                         symbol=code, side=side, status=LIVE, size=int(size), price=price)

    def _credit_order_type(self, tqc: Any, kind: CreditOrderKind, side: str) -> int:
        """信用委托类型 -> tqconst 编号。两道闸都在发单**之前**。"""
        if not self.is_credit:
            raise DriverError(f"账户类别 {self._client.account_type} 不能下信用委托 "
                              f"{kind.name}——融资/融券类委托只在 CREDIT 账户上合法")
        want = CREDIT_KIND_SIDE[kind]
        if side != want:
            raise DriverError(f"信用委托 {kind.name} 的方向必须是 {want}，收到 {side}")
        try:
            return getattr(tqc, kind.value)
        except AttributeError as e:
            raise DriverError(f"本机 tqconst 没有 {kind.value}，客户端版本可能过旧") from e

    # ---- 撤单 ----
    @_vendor_errors(mutating=True)
    def cancel_order(self, *, symbol: str, order_id: str) -> Dict[str, Any]:
        """撤单，并**撤完再读一次、按事实定终态**。

        「我发了撤单」与「这笔没成交」是两件事，中间隔着一场会输的比赛（实测撤单中部分成交
        1300/20900 股）⇒ 整段循环必须在本服务里跑完，调用方只看最终事实。
        不信回执文案（内嵌模拟常误报"提交撤单失败"，实际异步几秒后生效）。

        🔴 循环里「查不到」不算已撤：查询抖一下就把一笔还活着的委托记成已撤，
        调用方随即重下 ⇒ 双份成交。判不了就继续等，等到超时按未确认交回。
        """
        self.connect()
        self._client.cancel(to_tq_code(symbol), order_id)
        deadline = time.monotonic() + self._cancel_timeout
        while True:
            try:
                o = self.get_order(symbol=symbol, order_id=order_id)
            except (QueryUnavailable, DriverError) as e:
                # 🔴 确认查询本身失败也只是「本轮判不了」。撤单已经提交出去了，
                #    这时候往上抛「通道不可用」，调用方会以为什么都没发生。
                logger.warning("[tdx] 撤单确认这一轮查不到（%s），继续等", str(e)[:120])
                o = _QUERY_BLIP
            if o is not _QUERY_BLIP:
                if o is None:
                    return {"canceled": True, "order": None, "reason": "不在簿"}
                if o["status"] in (CANCELED, "expired"):
                    return {"canceled": True, "order": o, "reason": "柜台已记已撤"}
                if o["status"] == FILLED:
                    # 输掉了比赛：撤单发出去了，这笔却已经成交。**按事实回报**，
                    # 不要因为"我发过撤单"就说它撤掉了。
                    return {"canceled": False, "order": o, "reason": "撤单期间已全部成交"}
            if time.monotonic() >= deadline:
                last = None if o is _QUERY_BLIP else o
                return {"canceled": False, "order": last,
                        "reason": f"{self._cancel_timeout:.0f} 秒内没等到已撤"
                                  f"（状态未定，调用方须重新观测）"}
            time.sleep(self._cancel_interval)

    # ---- 查询 ----
    @_vendor_errors()
    def get_order(self, *, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        """按委托编号在当日委托里找。`None` ＝不在簿（多半已撤/隔日）。

        Raises:
            QueryUnavailable: 查询「成功但没内容」——它什么都说明不了，与"不在簿"相反。
        """
        for o in self._raw_orders():
            if str(o.get("Wtbh")) == str(order_id):
                return self._order_row(o, default_symbol=symbol)
        return None

    @_vendor_errors()
    def get_orders(self) -> List[Dict[str, Any]]:
        """当日全部委托。对账用（逐笔查每次都要拉一遍全量）。"""
        return [self._order_row(o) for o in self._raw_orders()]

    def _raw_orders(self) -> List[Dict[str, Any]]:
        self.connect()
        raw = self._client.query_orders("")
        if raw is None:
            raise QueryUnavailable("当日委托查询返回空内容——「查不到」不是「今天没有委托」")
        return raw

    def _order_row(self, o: Dict[str, Any], *, default_symbol: str = "") -> Dict[str, Any]:
        code = s(o, "Code", "Zqdm", "StockCode") or default_symbol
        return order_row(order_id=str(o.get("Wtbh")), symbol=to_tq_code(code),
                         side=order_side(o), status=order_status(o),
                         size=f(o, "WtVol"), price=f(o, "WtPrice"),
                         filled_size=f(o, "CjVol"), avg_fill_price=f(o, "CjPrice"))

    @_vendor_errors()
    def get_positions(self) -> List[Dict[str, Any]]:
        """持仓。**现价/市值/浮动盈亏恒空**——券商不返回，调用方用行情补算。"""
        out = []
        for p in self._raw_positions():
            size = f(p, "TotalVol", "Zqsl", "Ccsl", "StockVol", "Volume")
            if size <= 0:
                continue                       # 已清仓/无效行不进账本
            out.append(position_row(
                symbol=self._code(p), size=size,
                avg_price=f(p, "Cbj", "Cbjg", "CostPrice", "AvgPrice"),
                mark_price=f(p, "Zxjg", "Zxj", "MarketPrice", "NowPrice") or None,
                unrealized_pnl=f(p, "Ykje", "Ykyk", "ProfitLoss"),
                sellable=f(p, "CanUseVol", "KyVol", "Kysl")))
        return out

    @_vendor_errors()
    def get_sellable(self) -> Dict[str, Optional[str]]:
        """`{代码: 可卖股数}`（`CanUseVol`，T+1 当日买入为 0）。

        🔴 判不了一律抛 `QueryUnavailable`，**绝不折成空表**：空表被当成事实的后果是
        「每只都不能卖」⇒ 卖出腿一股不报（调仓那条腿是整天不换仓，正T 那条腿是
        当日融资买入不还款＝负债过夜）。
        """
        return {self._code(p): num(f(p, "CanUseVol", "KyVol", "Kysl"))
                for p in self._raw_positions()}

    def _raw_positions(self) -> List[Dict[str, Any]]:
        self.connect()
        raw = self._client.query_positions()
        if raw is None:
            raise QueryUnavailable("持仓查询返回空内容——「查不到」不是「空仓」")
        return raw

    @staticmethod
    def _code(row: Dict[str, Any]) -> str:
        return to_tq_code(s(row, "Code", "Zqdm", "StockCode"))

    @_vendor_errors()
    def get_account(self) -> Optional[Dict[str, Any]]:
        """账户资产。**一个资金字段都没有时返回 `None`，而不是一个全零的账户**：
        实测有 `{"ErrorId": "0", "Value": []}` 这种形态，`if not a` 拦不住（dict 非空），
        照旧往下走会把它读成权益 0 —— 取不到与权益是 0 是两件事。
        """
        self.connect()
        a = self._client.query_asset()
        if not a or not any(k in a for k in _MONEY_KEYS):
            return None
        return account_row(total_equity=f(a, "Asset"), total_available=f(a, "Cash"),
                           total_margin=f(a, "TotalMargin"),
                           total_unrealized_pnl=f(a, "ProfitLoss"),
                           currency=s(a, "Currency", default="CNY"),
                           cash_balance=f(a, "Balance"), frozen=f(a, "TotalFreeze"))

    # ---- 行情与标的 ----
    @_vendor_errors()
    def quotes(self, codes: Sequence[str]) -> List[Dict[str, Any]]:
        """快照。**厂商没有批量接口**，N 个代码就是 N 次调用，别拿它扫全市场。

        取不到的代码不出现在结果里（判不了 != 没有这只票）。
        """
        self.connect()
        out = []
        for raw in codes:
            code = to_tq_code(raw)
            snap = self._client.query_snapshot(code)
            if not snap:
                continue
            out.append(quote_row(symbol=code, last=f(snap, "Now"),
                                 prev_close=f(snap, "LastClose"),
                                 bid1=self._level(snap, "Buyp"),
                                 ask1=self._level(snap, "Sellp")))
        return out

    @staticmethod
    def _level(snap: Dict[str, Any], key: str) -> float:
        """盘口字段可能是五档数组，取第一档。"""
        v = snap.get(key)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else 0
        try:
            return float(str(v).strip() or 0)
        except (TypeError, ValueError):
            return 0.0

    @_vendor_errors()
    def instrument(self, code: str) -> Optional[Dict[str, Any]]:
        """标的静态信息。`margin_target` 三态：True / False / `None`＝判不了。

        🔴 它只回答**能不能融资买入**（`BelongRZRQ`）。担保品名单是另一回事——301616 被拒
        「非担保品标的」时 BelongRZRQ 仍是 1，别拿这一个字段去挡担保品买入。
        """
        self.connect()
        tq_code = to_tq_code(code)
        info = self._client.query_stock_info(tq_code)
        if not info:
            return None
        raw = info.get("BelongRZRQ")
        target: Optional[bool] = None
        if raw is not None and str(raw).strip() != "":
            try:
                target = bool(int(float(str(raw).strip())))
            except (TypeError, ValueError):
                logger.warning("[tdx] BelongRZRQ 读不出来 %s: %r", tq_code, raw)
        return {"symbol": tq_code, "name": s(info, "Name", "Zqmc"),
                "margin_target": target}

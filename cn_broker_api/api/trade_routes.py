"""交易那七个端点（`TradingPort` 的投影）。

账户维度作为显式业务字段传（`account` + `account_type`），不放 header——本服务靠它分组排队。
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from cn_broker_api.api.context import ApiContext
from cn_broker_api.api.request_validation import (
    finite_number,
    integer,
    json_object,
    optional_string,
    string,
)
from cn_broker_api.api.trade_call import account_of, in_queue, maps_failures
from cn_broker_api.drivers.base import require
from cn_broker_api.drivers.capability import Capability
from cn_broker_api.trade.credit_kind import parse_credit_kind
from cn_broker_api.trade.wire import known


def register(app: Flask, ctx: ApiContext) -> None:

    @app.post("/v1/orders")
    @maps_failures
    def create_order():  # noqa: ANN202
        """报单。201＝真报进柜台了；202＝推给客户端等确认（**不是拒单**）。"""
        body = json_object(request)
        account, account_type = account_of(body)
        symbol = string(body.get("symbol"), "symbol")
        if not symbol:
            raise ValueError("symbol 不能为空")
        credit_kind = parse_credit_kind(body.get("credit_kind"))
        if credit_kind is not None:
            require(ctx.driver, Capability.CREDIT_ORDER)
        # 字段校验在入口做完：不合法的请求不该先占一个账户串行槽再失败，而各驱动的严格程度
        # 并不一致（纸面驱动压根不校验）⇒ 400 由这里保证，驱动里那几道留作最后一闸。
        side = string(body.get("side"), "side").lower()
        if side not in ("buy", "sell"):
            raise ValueError(f"side 只能是 buy / sell，收到 {body.get('side')!r}")
        size = integer(body.get("size"), "size", minimum=1)
        price = finite_number(body.get("price"), "price")
        notify = (None if body.get("notify") is None
                  else integer(body.get("notify"), "notify"))
        order_type = string(body.get("order_type"), "order_type", default="limit")
        if order_type != "limit":
            raise ValueError("order_type 目前只支持 limit")
        client_order_id = optional_string(body.get("client_order_id"), "client_order_id")

        row = in_queue(ctx, account, account_type, f"报单 {symbol}",
                       lambda t: t.create_order(
                           symbol=symbol, side=side, size=size,
                           price=price, order_type=order_type,
                           client_order_id=client_order_id,
                           credit_kind=credit_kind, notify=notify))
        return jsonify(order=row), 201

    @app.delete("/v1/orders/<order_id>")
    @maps_failures
    def cancel_order(order_id: str):  # noqa: ANN202
        """撤单。**「撤完再读一次、按事实定终态」整段在本服务里跑完**，调用方只看结论。

        `outcome` 三取一：`canceled` 撤掉了 / `filled` 撤单期间已成交（那场比赛输了）/
        `timeout` **超时没等到，状态未定**。后两种的 `canceled` 都是 `false`，但只有
        `filled` 是已知事实；`timeout` 不许当撤单失败记，须重新观测柜台。
        """
        account, account_type = account_of()
        symbol = str(request.args.get("symbol") or "").strip()
        if not symbol:
            raise ValueError("撤单要给 symbol（厂商按代码+编号定位委托）")
        res = in_queue(ctx, account, account_type, f"撤单 {order_id}",
                       lambda t: t.cancel_order(symbol=symbol, order_id=order_id))
        return jsonify(res), 200

    @app.get("/v1/orders/<order_id>")
    @maps_failures
    def get_order(order_id: str):  # noqa: ANN202
        """单笔委托。`order: null` ＝不在当日委托簿里；查不到走 `known: false`。"""
        account, account_type = account_of()
        symbol = str(request.args.get("symbol") or "").strip()
        row = in_queue(ctx, account, account_type, f"查委托 {order_id}",
                       lambda t: t.get_order(symbol=symbol, order_id=order_id))
        return jsonify(known=True, order=row), 200

    @app.get("/v1/orders")
    @maps_failures
    def get_orders():  # noqa: ANN202
        """当日全部委托，对账用。"""
        account, account_type = account_of()
        rows = in_queue(ctx, account, account_type, "查当日委托", lambda t: t.get_orders())
        return jsonify(known(rows)), 200

    @app.get("/v1/positions")
    @maps_failures
    def get_positions():  # noqa: ANN202
        """持仓。**空表是「真的空仓」**，判不了是 `known: false` —— 混掉的后果是对账把
        持仓账本整表删了。"""
        account, account_type = account_of()
        rows = in_queue(ctx, account, account_type, "查持仓", lambda t: t.get_positions())
        return jsonify(known(rows)), 200

    @app.get("/v1/positions/sellable")
    @maps_failures
    def get_sellable():  # noqa: ANN202
        """可卖量。三态口径最要紧的一处：它在卖券还款腿和调仓卖出腿上各犯过一次
        「空表当真 0」，后果分别是负债过夜与那天等于没调仓。"""
        account, account_type = account_of()
        vols = in_queue(ctx, account, account_type, "查可卖量", lambda t: t.get_sellable())
        return jsonify(known=True, volumes=vols), 200

    @app.get("/v1/account")
    @maps_failures
    def get_account():  # noqa: ANN202
        """账户资产。取不到走 `known: false`——**不返回一个全零的账户**（探活会显示 0、
        调仓预算会算出 0 手）。"""
        account, account_type = account_of()
        row = in_queue(ctx, account, account_type, "查资产", lambda t: t.get_account())
        if row is None:
            return jsonify(known=False,
                           reason="资产查询没有任何资金字段——取不到，不是权益为 0"), 200
        return jsonify(known=True, account=row), 200

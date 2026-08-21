"""行情与标的两个端点。

⚠️ 这里的行情是**客户端自己那条**（报单会被它的状态挡住），不是公网行情源。项目里别处的
日线与快照走自研 socket 直连，与客户端毫无关系，所以这两个端点不是那些地方的替代品。
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from cn_broker_api.api.context import ApiContext
from cn_broker_api.api.trade_call import account_of, in_queue, maps_failures
from cn_broker_api.trade.wire import known

#: 一次最多问几个代码。**厂商没有批量接口**，N 个代码就是 N 次调用，
#: 而这些调用与报单共用同一条串行连接 ⇒ 名单长了会把下单挤在后面。
MAX_CODES = 50


def register(app: Flask, ctx: ApiContext) -> None:

    @app.get("/v1/quotes")
    @maps_failures
    def quotes():  # noqa: ANN202
        """快照（现价 / 昨收 / 买一 / 卖一）。取不到的代码不出现在结果里。

        `depth=true` 再带上**五档盘口含挂单量**与内外盘、最新单量、笔数、均价。
        ⭐ 封板时买一那档的挂单量就是**封单量**，这是要 depth 的主要理由。
        ⚠️ 这个端点走交易客户端（要账户句柄）。不需要盘口时用 `/v1/prices`：那个一次调用
        拿一批、也不要账户句柄。
        """
        codes = [c.strip() for c in (request.args.get("codes") or "").split(",") if c.strip()]
        if not codes:
            raise ValueError("要给 codes（逗号分隔，代码带不带后缀都行）")
        if len(codes) > MAX_CODES:
            raise ValueError(f"一次最多 {MAX_CODES} 个代码，收到 {len(codes)} 个")
        depth = (request.args.get("depth") or "").lower() in ("1", "true", "yes")
        account, account_type = account_of()
        rows = in_queue(ctx, account, account_type, f"快照 x{len(codes)}",
                        lambda t: t.quotes(codes, depth=depth))
        return jsonify(known(rows)), 200

    @app.get("/v1/instruments/<code>")
    @maps_failures
    def instrument(code: str):  # noqa: ANN202
        """标的静态信息。`margin_target` 三态（true / false / null＝判不了），
        **只回答能不能融资买入**——担保品名单是另一份名单，别拿它挡担保品买入。"""
        account, account_type = account_of()
        row = in_queue(ctx, account, account_type, f"标的 {code}",
                       lambda t: t.instrument(code))
        if row is None:
            return jsonify(known=False, reason=f"查不到 {code} 的标的信息"), 200
        return jsonify(known=True, instrument=row), 200

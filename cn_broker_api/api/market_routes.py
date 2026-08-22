"""行情与静态数据的端点。**不带账户维度**——这些调用不认账户，交易没登也能用。

 这里给的是**客户端自己那条行情**（报单会被它的状态挡住），不是公网行情源的替代品。
调用方那侧的日线回补、全市场快照走的是自研 socket 源，与这几个端点无关。
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from cn_broker_api.api.context import ApiContext
from cn_broker_api.api.trade_call import in_market_queue, maps_failures
from cn_broker_api.drivers.base import require
from cn_broker_api.drivers.capability import Capability
from cn_broker_api.drivers.tdxquant.market import normalize_codes, validate_klines
from cn_broker_api.trade.wire import known


def register(app: Flask, ctx: ApiContext) -> None:

    def _codes():
        raw = [c.strip() for c in (request.args.get("codes") or "").split(",") if c.strip()]
        if not raw:
            raise ValueError("要给 codes（逗号分隔，代码带不带后缀都行）")
        require(ctx.driver, Capability.MARKET_DATA)
        return normalize_codes(raw)

    @app.get("/v1/klines")
    @maps_failures
    def klines():  # noqa: ANN202
        """**最近 count 根** K 线。

         刻意不提供起止区间：实测给了 `start_time`/`end_time` 只回总数、行是空的
        （猜过三种分页参数名都不行）⇒ 这条路不是历史回补的路，别照着"有个 K 线接口"去改
        夜间回补。
         分钟线**不刷缓存基本就是空的** ⇒ 要分钟线就带 `refresh=true`（慢几秒）。
         单位随响应的 `units` 一起给：K 线的成交量是**股**、成交额是**万元**，
        而 `/v1/prices` 与快照那边成交量是**手**。同一个客户端里就不统一。
        """
        codes = _codes()
        period = request.args.get("period") or "1d"
        count = request.args.get("count") or "60"
        refresh = (request.args.get("refresh") or "").lower() in ("1", "true", "yes")
        dividend_type = request.args.get("dividend_type") or None
        if not str(count).lstrip("-").isdigit():
            raise ValueError(f"count 要是整数，收到 {count!r}")
        # 校验与驱动无关（纸面驱动也要被拦），所以在这一层先过一遍同一个函数。
        codes, period, n = validate_klines(codes, period, int(count))
        got = in_market_queue(ctx, f"K线 {period} x{len(codes)}",
                              lambda m: m.klines(codes, period=period, count=n,
                                                 refresh=refresh,
                                                 dividend_type=dividend_type))
        return jsonify(known=True, **got), 200

    @app.get("/v1/prices")
    @maps_failures
    def prices():  # noqa: ANN202
        """一次调用拿一批的现价 / 昨收 / 成交量（**手**）。

         与 `/v1/quotes` 的分工：这个是一次厂商调用拿 N 只，但**没有盘口**；
        要买一卖一或五档就用 quotes（那个只能逐个代码问）。
        """
        codes = _codes()
        rows = in_market_queue(ctx, f"批量报价 x{len(codes)}", lambda m: m.prices(codes))
        return jsonify(known=True, units={"volume": "lots"}, rows=rows), 200

    @app.get("/v1/limit-status")
    @maps_failures
    def limit_status():  # noqa: ANN202
        """封板状态：封单量、首次/最后封板时刻、开板次数。

         名字是「状态」而不是「涨跌停价」——**它不给价格**，价格仍要按板别与昨收自己算。
         字段是厂商的原始键名，语义厂商没写、本服务不猜（猜错一个键就会把「没封板」
        读成「封住了」）。
        """
        codes = _codes()
        rows = in_market_queue(ctx, f"封板状态 x{len(codes)}",
                               lambda m: m.limit_status(codes))
        return jsonify(known(rows)), 200

    @app.get("/v1/dividends/<code>")
    @maps_failures
    def dividends(code: str):  # noqa: ANN202
        """除权除息事件清单。`start` / `end` 是 `YYYYMMDD`，由**本服务**过滤
        （厂商源码里写着那两个参数在 C 接口上没有实际作用）。

         四个数值字段照抄厂商的列序，**单位没实测过** ⇒ 拿它算复权因子之前先与已有来源
        对一遍。日期清单本身是可靠的，交叉核对复权因子时最有用的也是它。
        """
        require(ctx.driver, Capability.MARKET_DATA)
        start = (request.args.get("start") or "").strip()
        end = (request.args.get("end") or "").strip()
        rows = in_market_queue(ctx, f"除权除息 {code}",
                               lambda m: m.dividends(code, start=start, end=end))
        return jsonify(known(rows)), 200

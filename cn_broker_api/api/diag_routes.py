"""排查用的两个动作：收起窗口、抓当时那个窗口的位图。

位图是这套东西里排查性价比最高的一样：「它到底弹了个什么框」这个问题，文字日志答不了。
 但它会把账号、持仓、资产原样拍进去 ⇒ 要 token、只在内存里过一遍、绝不落盘。
"""
from __future__ import annotations

from flask import Flask, Response, jsonify

from cn_broker_api.api.context import ApiContext
from cn_broker_api.api.trade_call import maps_failures
from cn_broker_api.drivers.base import require
from cn_broker_api.drivers.capability import Capability


def register(app: Flask, ctx: ApiContext) -> None:

    @app.post("/v1/session/minimize")
    @maps_failures
    def minimize():  # noqa: ANN202
        """把客户端主窗口收起来。返回收了几个（0 ＝本来就没有可见的主窗口）。"""
        require(ctx.driver, Capability.DESKTOP_DIAG)
        n = ctx.driver.minimize_desktop()
        return jsonify(minimized=n), 200

    @app.get("/v1/diag/screenshot")
    @maps_failures
    def screenshot():  # noqa: ANN202
        """当时那个窗口的位图（PNG）。有登录框就抓登录框。

         **不落盘**：这张图里有账号和资产。`Cache-Control: no-store` 是同一个理由——
        别让它留在浏览器缓存里。
        """
        require(ctx.driver, Capability.DESKTOP_DIAG)
        png, what = ctx.driver.screenshot()
        return Response(png, mimetype="image/png",
                        headers={"Cache-Control": "no-store",
                                 "X-Window": what.encode("ascii", "backslashreplace")
                                 .decode("ascii")})

"""通道自检的两个端点。

 `GET` **必须便宜**：读缓存 + 回报年龄。「交易账号登录了没」那一项要连客户端、
查一次资产，几秒钟且占用串行槽——诊断页每 5 秒轮询一次的话，不缓存等于整天骚扰交易通道。
只有 `POST /v1/health/refresh` 才真去探。
"""
from __future__ import annotations

from flask import Flask, jsonify

from cn_broker_api.api.context import ApiContext
from cn_broker_api.api.probe import cache_key as _cache_key
from cn_broker_api.api.probe import probe


def register(app: Flask, ctx: ApiContext) -> None:
    cache, driver = ctx.cache, ctx.driver

    def _probe():  # noqa: ANN202
        return probe(driver)

    @app.get("/v1/health")
    def health():  # noqa: ANN202
        """**便宜**：读缓存 + 回报年龄，没有缓存时才真探一次。

         缓存按 (账号, 类别, need_times) 分键：共用一份会让 A 读到"按 B 的时刻判"的结论。
        """
        e = cache.fresh(key=_cache_key())
        fresh_now = False
        if e is None:
            e = cache.put(_probe(), key=_cache_key())
            fresh_now = True
        body = dict(e.payload)
        body["age_seconds"] = round(e.age_seconds(), 1)
        body["from_cache"] = not fresh_now
        body["cache_ttl_seconds"] = cache.ttl
        return jsonify(body)

    @app.post("/v1/health/refresh")
    def health_refresh():  # noqa: ANN202
        """强制重探。**要连客户端、占串行槽 ⇒ 只能按需打**（诊断页那个按钮）。"""
        e = cache.put(_probe(), key=_cache_key())
        body = dict(e.payload)
        body["age_seconds"] = 0.0
        body["from_cache"] = False
        return jsonify(body)

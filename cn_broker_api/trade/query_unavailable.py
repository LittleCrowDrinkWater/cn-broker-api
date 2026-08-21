"""查询「成功但没内容」。"""
from __future__ import annotations

from cn_broker_api.drivers.driver_error import DriverError


class QueryUnavailable(DriverError):
    """判不了，**不是空结果**。

    线上表达是 `{"known": false, "reason": ...}`（仍是 200）。丢掉这一位的症状不是报错，
    是某天早上对账把持仓账本整表删了、执行那侧从零重新建仓。
    """

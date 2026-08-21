"""调用超时，状态未知。"""
from __future__ import annotations

from cn_broker_api.drivers.driver_error import DriverError


class AckUnknown(DriverError):
    """请求发出去了但没等到答复。线上表达 `504`。

    🔴 与「通道不可用」（503）必须分开：**报单超时不代表没报出去**。调用方收到 504 的
    唯一正确动作是去柜台重新观测，而不是当成没报成再挂一批。
    """

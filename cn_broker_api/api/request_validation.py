"""HTTP JSON 请求字段的严格解析。

交易接口不接受 Python 式的宽松转换。例如 ``1.5`` 不能截断成 1，布尔值也不能
作为整数使用；否则调用方发送的内容与实际进入柜台的内容可能不同。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

from flask import Request


def json_object(request: Request, *, allow_empty: bool = False) -> Dict[str, Any]:
    """读取 JSON 对象；数组、标量和无法解析的请求体均视为参数错误。"""
    value = request.get_json(silent=True)
    if value is None and allow_empty and not request.get_data(cache=True):
        return {}
    if not isinstance(value, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return value


def string(value: Any, field: str, *, default: Optional[str] = None) -> str:
    """读取字符串字段，不把列表、数字或布尔值隐式转成文本。"""
    if value is None and default is not None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    return value.strip()


def optional_string(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return string(value, field)


def integer(
    value: Any,
    field: str,
    *,
    default: Optional[int] = None,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """读取 JSON 整数；明确排除 ``bool``，因为它是 Python 的 ``int`` 子类。"""
    if value is None and default is not None:
        result = default
    elif isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 必须是整数")
    else:
        result = value
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{field} 不能大于 {maximum}")
    return result


def finite_number(value: Any, field: str, *, minimum_exclusive: float = 0.0) -> float:
    """读取有限 JSON 数值，拒绝字符串、布尔值、NaN 与无穷大。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数字")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} 必须是有限数字")
    if result <= minimum_exclusive:
        raise ValueError(f"{field} 必须大于 {minimum_exclusive:g}")
    return result


def boolean(value: Any, field: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} 必须是布尔值")
    return value


def account_type(value: Any, *, default: str = "STOCK") -> str:
    result = string(value, "account_type", default=default).upper()
    if result not in {"STOCK", "CREDIT"}:
        raise ValueError("account_type 只能是 STOCK 或 CREDIT")
    return result

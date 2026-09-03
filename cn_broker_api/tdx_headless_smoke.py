"""只读探测 headless TPyth 端口；不包含任何下单或撤单方法。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any

from cn_broker_api.stdio import init_stdio


def _call(url: str, sequence: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": sequence, "method": method, "params": params}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict) or body.get("error"):
        raise RuntimeError(f"{method} 返回了无效的 JSON-RPC 响应")
    result = body.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{method} 的 result 不是对象")
    return result


def _is_ok(result: dict[str, Any]) -> bool:
    return str(result.get("ErrorId", "0")) == "0"


def main() -> int:
    init_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:17711/")
    parser.add_argument("--account-type", default="STOCK")
    args = parser.parse_args()

    # 账号只从环境变量进入，并且永不打印；空值让厂商端选择当前登录账户。
    account = os.environ.get("TDX_LAB_ACCOUNT", "").strip()
    account_result = _call(
        args.url,
        1,
        "stock_account",
        {"account": account, "account_type": args.account_type.upper()},
    )
    handle = account_result.get("Value") if _is_ok(account_result) else None
    print(f"资金账户查询成功={handle is not None}")
    if not isinstance(handle, (int, float)):
        return 2

    asset = _call(args.url, 2, "query_stock_asset", {"account_id": int(handle)})
    positions = _call(
        args.url, 3, "query_stock_positions", {"account_id": int(handle)}
    )
    orders = _call(
        args.url,
        4,
        "query_stock_orders",
        {"account_id": int(handle), "stock_code": ""},
    )
    position_rows = positions.get("Value")
    order_rows = orders.get("Value")
    print(
        "只读探测 "
        f"资产成功={_is_ok(asset)} "
        f"持仓成功={_is_ok(positions)} "
        f"持仓条数={len(position_rows) if isinstance(position_rows, list) else -1} "
        f"委托成功={_is_ok(orders)} "
        f"委托条数={len(order_rows) if isinstance(order_rows, list) else -1}"
    )
    return 0 if _is_ok(asset) and _is_ok(positions) and _is_ok(orders) else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"只读探测失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)

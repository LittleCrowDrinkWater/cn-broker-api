"""CLI：启动不含完整 Tdxw.exe 的通达信交易宿主。"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from cn_broker_api.drivers.tdxquant.headless import HeadlessTradeHost, preflight
from cn_broker_api.stdio import init_stdio


def main() -> int:
    init_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="带 .trade-lab-marker 的实验副本")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-only", action="store_true", help="只执行启动前检查")
    mode.add_argument("--start", action="store_true", help="启动交易 DLL 宿主")
    parser.add_argument("--launch-tc", action="store_true", help="同时启动实验副本的 TC.exe")
    args = parser.parse_args()
    if args.launch_tc and not args.start:
        parser.error("--launch-tc 只能与 --start 一起使用")

    root, hq_port, py_port, mcp_port = preflight(args.root)
    print(
        f"启动前检查通过：root={root} HQMP={hq_port} PYMP={py_port} MCP={mcp_port}",
        flush=True,
    )
    if args.check_only:
        return 0

    host = HeadlessTradeHost(root, hq_port, py_port, mcp_port)
    try:
        host.start(launch_tc=args.launch_tc)
        print(f"headless 交易宿主已就绪：http://127.0.0.1:{mcp_port}/", flush=True)
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        host.stop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"headless 交易宿主失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)

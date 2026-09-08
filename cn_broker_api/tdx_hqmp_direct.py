"""CLI：不启动 Tdxw，直接对实验副本 TC 做一次只读 HQMP 探测。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cn_broker_api.drivers.tdxquant.hqmp_direct import HqmpDirectSession
from cn_broker_api.stdio import init_stdio


def main() -> int:
    init_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="带 .trade-lab-marker 的实验副本")
    parser.add_argument("--port", type=int, required=True, help="实验 TC 注册表路由指向的 HQMP 端口")
    parser.add_argument("--launch-tc", action="store_true", help="同时启动实验副本的 TC.exe")
    parser.add_argument("--connect-timeout", type=float, default=120.0)
    parser.add_argument("--call-timeout", type=float, default=20.0)
    args = parser.parse_args()

    host = HqmpDirectSession(args.root, args.port)
    try:
        host.start(launch_tc=args.launch_tc)
        print(f"HQMP 只读宿主已监听 127.0.0.1:{args.port}，等待实验 TC 注册", flush=True)
        host.wait_for_client(args.connect_timeout)
        summary = host.probe(args.call_timeout)
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")), flush=True)
        return 0
    finally:
        host.stop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"HQMP 只读探测失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)

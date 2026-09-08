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
    parser.add_argument("--capture", type=Path, required=True, help="含已核验调用模板的 HQMP 抓包")
    tc_mode = parser.add_mutually_exclusive_group()
    tc_mode.add_argument("--launch-tc", action="store_true", help="同时启动实验副本的 TC.exe")
    tc_mode.add_argument("--reuse-tc", action="store_true", help="接管已登录实验 TC 的重连")
    parser.add_argument("--connect-timeout", type=float, default=120.0)
    parser.add_argument("--call-timeout", type=float, default=20.0)
    args = parser.parse_args()

    host = HqmpDirectSession(args.root, args.port, args.capture)
    try:
        host.start(launch_tc=args.launch_tc, reuse_tc=args.reuse_tc)
        print(f"HQMP 只读宿主已监听 127.0.0.1:{args.port}，等待实验 TC 注册", flush=True)
        # 复用模式接管的是已经登录的 TC；一次性的登录就绪通知不会在重连后重发。
        host.wait_for_client(args.connect_timeout, require_ready=not args.reuse_tc)
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

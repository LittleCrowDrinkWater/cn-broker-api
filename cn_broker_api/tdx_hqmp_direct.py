"""CLI：不启动 Tdxw，直接对实验副本 TC 做一次只读 HQMP 探测。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cn_broker_api.drivers.tdxquant import login as desktop_login
from cn_broker_api.drivers.tdxquant.hqmp_direct import HqmpDirectSession
from cn_broker_api.state import SubmitLatch
from cn_broker_api.stdio import init_stdio


def _ensure_direct_login(host: HqmpDirectSession, args: argparse.Namespace) -> None:
    """复用桌面窗口登录，但以直接 HQMP 的账户和资产查询作为成功判据。"""
    already_ready, detail = host.channel_ok(min(1.0, args.call_timeout))
    if already_ready:
        print(detail, flush=True)
        return
    desktop_login.set_cred_path(args.cred_file)
    desktop_login.VERBOSE = True
    cred = desktop_login.load_cred()
    account = str(cred.get("account") or "")
    latch = SubmitLatch(
        args.login_state_dir,
        max_per_day=args.max_password_submits_per_day,
        max_consecutive_failures=args.max_consecutive_failures,
    )
    latch.claim(account)
    ok, detail = desktop_login.ensure_logged_in(
        cred,
        wait=int(args.connect_timeout),
        start=False,
        minimize=not args.keep_login_window,
        required_processes=("TC.exe",),
        channel_probe=lambda _: host.channel_ok(min(5.0, args.call_timeout)),
    )
    latch.settle(account, ok)
    if not ok:
        raise RuntimeError(detail)


def main() -> int:
    init_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="带 .trade-lab-marker 的实验副本")
    parser.add_argument("--port", type=int, required=True, help="实验 TC 注册表路由指向的 HQMP 端口")
    parser.add_argument("--capture", type=Path, required=True, help="含已核验调用模板的 HQMP 抓包")
    tc_mode = parser.add_mutually_exclusive_group()
    tc_mode.add_argument("--launch-tc", action="store_true", help="同时启动实验副本的 TC.exe")
    tc_mode.add_argument("--reuse-tc", action="store_true", help="接管已登录实验 TC 的重连")
    parser.add_argument("--auto-login", action="store_true", help="自动处理实验 TC 的交易登录窗口")
    parser.add_argument("--cred-file", type=Path, help="仓库外的交易登录凭据 JSON")
    parser.add_argument("--login-state-dir", type=Path, help="仓库外的密码提交次数状态目录")
    parser.add_argument("--max-password-submits-per-day", type=int, default=10)
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    parser.add_argument("--keep-login-window", action="store_true")
    parser.add_argument("--connect-timeout", type=float, default=120.0)
    parser.add_argument("--call-timeout", type=float, default=20.0)
    args = parser.parse_args()
    if args.auto_login and not args.launch_tc:
        parser.error("--auto-login 必须与 --launch-tc 一起使用")
    if args.auto_login and (args.cred_file is None or args.login_state_dir is None):
        parser.error("--auto-login 必须同时提供 --cred-file 和 --login-state-dir")

    host = HqmpDirectSession(args.root, args.port, args.capture)
    try:
        host.start(launch_tc=args.launch_tc, reuse_tc=args.reuse_tc)
        print(
            f"HQMP host listening | address=127.0.0.1:{args.port} state=waiting_for_tc",
            flush=True,
        )
        if args.auto_login:
            _ensure_direct_login(host, args)
        else:
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
        print(f"HQMP probe failed | {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)

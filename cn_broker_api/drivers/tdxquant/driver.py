"""桌面客户端驱动：把搬来的那几份（登录 / 自检 / 页面补丁 / 交易）装成一个驱动。

这一层**只做编排**，不含任何判据——判据全在 `login.py` / `health.py` / `trading.py` 里，
前两份是从生产环境整文件搬来的，动它们等于把真机验证的结果作废。

密码两条来源由配置的 `cred_source` 选（文件 / 请求下发），共用 `_resolve_cred()` 一个出口。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cn_broker_api.config import TdxQuantConfig
from cn_broker_api.drivers.capability import Capability
from cn_broker_api.drivers.capability_missing import CapabilityMissing
from cn_broker_api.drivers.desktop_recipe import DesktopRecipe
from cn_broker_api.drivers.driver_error import DriverError
from cn_broker_api.drivers.ensure_result import EnsureResult
from cn_broker_api.drivers.session_state import SessionState
from cn_broker_api.drivers.tdxquant import health as H
from cn_broker_api.drivers.tdxquant import login as L
from cn_broker_api.drivers.tdxquant.client import TdxQuantClient, set_pyplugins
from cn_broker_api.drivers.tdxquant.hqmp_direct import HqmpDirectSession
from cn_broker_api.drivers.tdxquant.hqmp_direct_trading import HqmpDirectTrading
from cn_broker_api.drivers.tdxquant.market import TdxQuantMarketData
from cn_broker_api.drivers.tdxquant.mcp import McpClient
from cn_broker_api.drivers.tdxquant.trading import TdxQuantTrading
from cn_broker_api.state import PasswordVault, SubmitLatch
from cn_broker_api.trade.query_unavailable import QueryUnavailable

logger = logging.getLogger(__name__)

#: 这一版客户端的桌面配方。**数据，不是代码**（见 `drivers/base.py` 那段）：
#: 换一个更精简的客户端版本，多半是照着写一份新的配方，而不是改机制。
#:
#: `processes` 的顺序就是**拉起顺序**：主程序先起（本地那个 JSON-RPC 端口是它开的），
#: 交易模块后起。
#: 交易模块**不会自己起来**：主程序开了自动登录之后，启动只登行情，交易那一半要人在
#: 界面上点一下【交易】才拉起 ⇒ 于是"既没登上、也没有登录框"这种谁都不动的僵局
#: （实测等 120 秒也没弹）。所以它必须列在这里，由我们自己拉。
TDX_RECIPE = DesktopRecipe(
    processes=("Tdxw.exe", "TC.exe"),
    executables={"Tdxw.exe": "Tdxw.exe", "TC.exe": str(Path("NewTc") / "TC.exe")},
)

# headless 宿主取代 Tdxw.exe 持有量化与 RPC DLL；桌面侧只剩登录和柜台连接所在的 TC.exe。
TDX_HEADLESS_RECIPE = DesktopRecipe(
    processes=("TC.exe",),
    executables={"TC.exe": str(Path("NewTc") / "TC.exe")},
)


class TdxQuantDriver:
    """真驱动。**Windows 专属**——非 Windows 上 import 得动，真去调才失败
    （在能跑的地方跑，在不能跑的地方明确失败，而不是 import 期就把服务拖挂）。"""

    name = "tdxquant"

    def __init__(self, cfg: TdxQuantConfig, *, latch: SubmitLatch,
                 vault: Optional[PasswordVault] = None) -> None:
        if cfg.desktop_mode == "headless":
            marker = cfg.tdx_home / ".trade-lab-marker" if cfg.tdx_home else None
            if marker is None or not marker.is_file():
                raise DriverError(
                    "desktop_mode = headless 只能使用带 .trade-lab-marker 的实验副本"
                )
        self.cfg = cfg
        self.latch = latch
        self.vault = vault or PasswordVault()
        self._desktop_recipe = (TDX_HEADLESS_RECIPE
                                if cfg.desktop_mode == "headless" else TDX_RECIPE)
        # 注入而不是让搬来的那两份各自读配置：两处各推一份的话换客户端时漏改一处，
        # 表现是自检把「文件不在」读成「补丁没打」。
        H.set_tdx_home(cfg.tdx_home)
        set_pyplugins((cfg.tdx_home / "PYPlugins") if cfg.tdx_home else None)
        L.set_mcp_url(cfg.mcp_url)
        L.set_cred_path(cfg.cred_file if cfg.cred_source == "file" else None)
        self._hqmp_session: Optional[HqmpDirectSession] = None
        if cfg.transport == "hqmp":
            if cfg.tdx_home is None or cfg.hqmp_capture is None:
                raise DriverError("HQMP 直接通道缺少实验副本或抓包模板路径")
            session = HqmpDirectSession(
                cfg.tdx_home,
                cfg.hqmp_port,
                cfg.hqmp_capture,
                enable_trade=cfg.hqmp_enable_trade,
            )
            try:
                # 监听必须早于登录接口启动 TC，否则 TC 连不到它的 HQMP 宿主。
                session.start(launch_tc=False, reuse_tc=cfg.hqmp_reuse_tc)
            except Exception as exc:  # noqa: BLE001 — 启动失败必须中止整个服务
                raise DriverError(f"启动直接 HQMP 宿主失败：{exc}") from exc
            self._hqmp_session = session

    # ── 能力 ─────────────────────────────────────────────
    def capabilities(self) -> List[str]:
        """行情那一族只在 mcp 通道上声明：它是照着客户端那个 JSON-RPC 端口写的，
        ctypes 通道上没实现。谎报的表现是调用方过了能力闸然后撞一个看不懂的错。"""
        if self._hqmp_session is not None:
            caps = [Capability.SELLABLE_VOLUME, Capability.DESKTOP_LOGIN,
                    Capability.DESKTOP_DIAG]
            if self.cfg.hqmp_enable_trade:
                caps[:0] = [Capability.CREDIT_ORDER, Capability.CANCEL]
            return caps
        caps = [Capability.CREDIT_ORDER, Capability.CANCEL, Capability.BID_ASK_QUOTE,
                Capability.SELLABLE_VOLUME, Capability.DESKTOP_LOGIN,
                Capability.DESKTOP_DIAG, Capability.AUTOCONFIRM_PATCH]
        if self.cfg.transport == "mcp":
            caps.insert(4, Capability.MARKET_DATA)
        return caps

    def market(self) -> TdxQuantMarketData:
        """行情与静态数据。**不取账户句柄**——实测这些函数都不认账户，
        交易没登也能用（而它们恰恰是交易登录出问题时最需要能看的东西）。"""
        if self._hqmp_session is not None:
            raise CapabilityMissing(Capability.MARKET_DATA, self.name)
        return TdxQuantMarketData(self.client())

    def client(self) -> McpClient:
        """自检那一侧的最小通道（只回答"通道通不通"，三十行）。"""
        return McpClient(self.cfg.mcp_url)

    def trading(self, *, account: str = "", account_type: str = "STOCK") -> Any:
        """这个账户上的交易与查询。

         现构现用：底下那条连接是**进程级共享**的，`connect()` 命中同一个身份就短路，
        所以这里没有要缓存的东西。身份含账号与类别（`ConnKey`），换账户才会真重连。
        """
        if self._hqmp_session is not None:
            return HqmpDirectTrading(
                self._hqmp_session,
                account=account,
                account_type=account_type,
                # 调用方可在报单时显式给 security_name；HQMP 本身没有
                # 已验证的代码到名称查询，所以这里不猜测也不偷连其他行情源。
                instrument_of=lambda _code: None,
                call_timeout=max(20.0, self.cfg.cancel_confirm_timeout),
                cancel_visibility_timeout=self.cfg.cancel_confirm_timeout,
                cancel_confirm_timeout=self.cfg.cancel_confirm_timeout,
                cancel_confirm_interval=self.cfg.cancel_confirm_interval,
                max_order_size=self.cfg.hqmp_max_order_size,
                max_order_notional=self.cfg.hqmp_max_order_notional,
            )
        client = TdxQuantClient(
            str(self.cfg.tdx_home / "PYPlugins") if self.cfg.tdx_home else None,
            account=account, account_type=account_type,
            transport=self.cfg.transport, mcp_url=self.cfg.mcp_url)
        return TdxQuantTrading(client, cancel_timeout=self.cfg.cancel_confirm_timeout,
                               cancel_interval=self.cfg.cancel_confirm_interval)

    # ── 桌面进程（看门狗要的两件事）─────────────────────
    def desktop_recipe(self) -> DesktopRecipe:
        return self._desktop_recipe

    def desktop_processes(self) -> Dict[str, bool]:
        """配方里那几个进程各自在不在跑。**只读、零成本**，不连客户端、不抢锁。

         「进程在跑」离「能下单」还有三道门 ⇒ 这个结果不能当成通道可用。
        """
        running = {name.lower() for name in
                   L.running_processes(self._desktop_recipe.processes).values()}
        return {n: (n.lower() in running) for n in self._desktop_recipe.processes}

    def start_desktop_process(self, name: str) -> None:
        """按配方拉起一个进程。**只拉起，绝不 kill**（见 `WatchdogConfig` 那段）。"""
        rel = self._desktop_recipe.executables.get(name)
        if not rel:
            raise DriverError(
                f"配方里没有 {name!r}，认得的是 {sorted(self._desktop_recipe.executables)}"
            )
        try:
            L._spawn(rel)
        except SystemExit as e:                # _spawn 找不到 exe 时 SystemExit(2)
            raise DriverError(f"起 {name} 失败（{rel} 找不到？）：{e}") from e

    # ── 排查用的两个动作 ─────────────────────────────────
    def minimize_desktop(self) -> int:
        """把客户端窗口收起来，返回收了几个。

         无人值守那一趟结束时才该收：人正看着它的时候动窗口很讨厌，
        所以 `ensure` 只在**真动手过**的那一趟收（见 `EnsureResult.acted`）。
        """
        return L.minimize_client(L._target_pids(self._desktop_recipe.processes))

    def screenshot(self) -> "tuple[bytes, str]":
        """抓当时那个窗口的位图，返回 (PNG 字节, 说明)。

         **只在内存里过一遍，绝不落盘**：位图会把账号、持仓、资产原样拍进去。
         有登录框就抓登录框——排查的时候要看的正是「它到底弹了个什么框」。
        """
        import io

        pids = L._target_pids(self._desktop_recipe.processes)
        if not pids:
            raise DriverError("客户端进程一个都不在跑，没有窗口可抓")
        dlg = L.find_login_dialog(pids)
        tops = L.visible_tops(pids)
        target, what = (dlg, "登录框") if dlg else (None, "")
        if target is None:
            if not tops:
                raise DriverError("客户端在跑但没有可见窗口（最小化了？）")
            target, what = next(iter(tops.items()))[0], "客户端主窗口"
        img = L.grab(target)
        if img is None:
            raise DriverError(f"抓 {what} 的位图失败（窗口刚关掉？）")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), f"{what}（hwnd={target}）"

    # ── 自检 ─────────────────────────────────────────────
    def health(self, *, account: str = "", account_type: str = "STOCK",
               need_times: Sequence[Tuple[int, int]] = ()) -> Dict[str, Any]:
        if self._hqmp_session is not None:
            ready, detail = self._direct_channel_probe(account)
            checks = [
                {"key": "transport", "name": "HQMP 宿主与 TC", "ok": ready,
                 "warn": False, "detail": detail},
                {"key": "account", "name": "交易账号登录", "ok": ready,
                 "warn": False, "detail": detail},
                {"key": "trade_gate", "name": "HQMP 交易闸", "ok": True,
                 "warn": not self.cfg.hqmp_enable_trade,
                 "detail": ("已显式打开，仍受单笔数量和金额上限保护"
                            if self.cfg.hqmp_enable_trade
                            else "当前只读，不接受报单或撤单")},
                {"key": "autoconfirm", "name": "页面自动确认", "ok": True,
                 "warn": False, "detail": "直接 HQMP 不经过页面确认队列，无需补丁"},
            ]
            return {"ok": ready, "checks": checks,
                    "message": "直接 HQMP 已就绪" if ready else detail}
        h = H.check_trade_channel(self.client(), account=account,
                                  account_type=account_type, need_times=need_times,
                                  probe_symbol=H.DEFAULT_PROBE_SYMBOL)
        return {
            "ok": h.ok,
            "message": h.message(),
            "checks": [{"key": c.key, "name": c.name, "ok": c.ok,
                        "warn": c.warn, "detail": c.detail} for c in h.checks],
        }

    def _direct_channel_probe(self, account: str) -> Tuple[bool, str]:
        assert self._hqmp_session is not None
        timeout = min(5.0, max(1.0, self.cfg.cancel_confirm_timeout))
        ready, detail = self._hqmp_session.channel_ok(timeout)
        if not ready:
            return False, detail
        try:
            self._hqmp_session.require_account(account, timeout=timeout)
        except (QueryUnavailable, ValueError) as exc:
            return False, str(exc)
        return True, detail

    # ── 登录 ─────────────────────────────────────────────
    def _resolve_cred(self, *, password: Optional[str], account: str,
                      account_type: str) -> Dict[str, str]:
        """凑出 `login` 那一侧要的凭据字典。两条来源共用一个出口（见模块 docstring）。"""
        if self.cfg.cred_source == "request":
            pw = password or self.vault.get(account)
            if not pw:
                raise DriverError(
                    f"账户 {account or '(默认)'} 手上没有密码。"
                    f'本服务按 cred_source = "request" 配置，密码应当随请求下发；'
                    f"重启之后内存里的那份就没了（刻意的：密码不落盘）")
            if password:
                # 这一趟带了密码就存下来，让白天的重登和诊断页那个按钮有东西可用。
                self.vault.put(account, password)
            return {"account": account, "password": pw, "account_type": account_type}

        cred = L.load_cred()          # 抛 CredMissing，由 HTTP 层翻译
        # 请求里给了账号就以请求为准：一台机器上可能有多个账户，而文件里只写了一个。
        if account:
            cred = {**cred, "account": account, "account_type": account_type}
        return cred

    def ensure_logged_in(self, *, password: Optional[str] = None,
                         account: str = "", account_type: str = "STOCK",
                         start: bool = True, minimize: bool = True,
                         wait_seconds: int = 240) -> EnsureResult:
        if self._hqmp_session is not None:
            return self._ensure_direct_logged_in(
                password=password,
                account=account,
                account_type=account_type,
                start=start,
                minimize=minimize,
                wait_seconds=wait_seconds,
            )
        cred = self._resolve_cred(password=password, account=account,
                                  account_type=account_type)
        acc = str(cred.get("account") or "")

        # 先看是不是已经好了，好了直接返回，不占密码额度（状态驱动而非弹框驱动）。
        ok, detail = L.channel_ok(cred)
        if ok:
            return EnsureResult(ok=True, acted=False, detail=detail)

        # 要走到填密码那一步了，先要额度。**先记后点**（见 state 模块 docstring）。
        self.latch.claim(acc)
        try:
            ok, detail = L.ensure_logged_in(
                cred, wait=wait_seconds, start=start, minimize=minimize,
                required_processes=self._desktop_recipe.processes)
        except SystemExit as e:        # login._spawn 找不到 exe 时会 SystemExit(2)
            # 刻意不结算：`claim()` 已按失败记过，这里让它留着——起不来客户端与密码错在这一层
            # 分不开，保守那边的代价小得多。
            raise DriverError(f"起客户端失败：{e}") from e
        # 必须结算：不调的表现是「登录成功了但连续失败计数一直涨」，几天后那个闸会自己关死。
        self.latch.settle(acc, ok)
        return EnsureResult(ok=ok, detail=detail, acted=True)

    def _ensure_direct_logged_in(self, *, password: Optional[str], account: str,
                                 account_type: str, start: bool, minimize: bool,
                                 wait_seconds: int) -> EnsureResult:
        """复用桌面登录机制，但只以 HQMP 账户和资产查询作为成功判据。"""
        assert self._hqmp_session is not None
        # 已就绪时不要读取或解密凭据；只读探测足以证明当前请求的账户可用。
        ready, detail = self._direct_channel_probe(account)
        if ready:
            if account:
                # 前一次登录若在 HQMP 注册前超时，闩已保守记作失败；后来确认同一账户
                # 已就绪时必须清零，否则连续几次慢注册会把正确密码永久锁住。
                self.latch.settle(account, True)
            return EnsureResult(ok=True, acted=False, detail=detail)

        cred = self._resolve_cred(
            password=password, account=account, account_type=account_type
        )
        expected_account = str(cred.get("account") or "")

        self.latch.claim(expected_account)
        try:
            ok, detail = L.ensure_logged_in(
                cred,
                wait=wait_seconds,
                start=start,
                minimize=minimize,
                required_processes=self._desktop_recipe.processes,
                channel_probe=lambda _: self._direct_channel_probe(expected_account),
            )
        except SystemExit as exc:
            raise DriverError(f"起实验 TC 失败：{exc}") from exc
        self.latch.settle(expected_account, ok)
        return EnsureResult(ok=ok, detail=detail, acted=True)

    def session_status(self, *, account: str = "",
                       account_type: str = "STOCK") -> Dict[str, Any]:
        """只观察登录状态；识别不充分时不猜成“未登录”。"""
        if self._hqmp_session is not None:
            return self._direct_session_status(account)
        cred = {"account": account, "account_type": account_type}
        ok, _detail = L.channel_ok(cred)
        if ok:
            return {
                "state": SessionState.READY.value,
                "ready": True,
                "detail": "交易账户和资产查询已通过",
            }

        try:
            pids = L._target_pids(self._desktop_recipe.processes)
            if "TC.exe" not in pids.values():
                return {
                    "state": SessionState.LOGIN_REQUIRED.value,
                    "ready": False,
                    "detail": "交易内核 TC.exe 未启动；可调用登录接口启动并登录",
                }
            dialog = L.find_login_dialog(pids)
            if dialog is not None:
                kind = L.classify(L.snapshot(dialog))
                if kind == "trade":
                    return {
                        "state": SessionState.LOGIN_REQUIRED.value,
                        "ready": False,
                        "detail": "已识别到交易登录窗口；可调用登录接口完成登录",
                    }
                return {
                    "state": SessionState.MANUAL_ACTION_REQUIRED.value,
                    "ready": False,
                    "detail": f"检测到尚未识别的客户端窗口类型：{kind}",
                }
        except Exception as exc:  # noqa: BLE001 — 状态观察必须返回未知，不能猜成未登录
            return {
                "state": SessionState.CHANNEL_UNAVAILABLE.value,
                "ready": False,
                "detail": f"检查交易登录窗口失败：{type(exc).__name__}",
            }
        return {
            "state": SessionState.CHANNEL_UNAVAILABLE.value,
            "ready": False,
            "detail": "交易内核运行中，但账户与资产查询未通过，且没有明确的登录窗口",
        }

    def _direct_session_status(self, account: str) -> Dict[str, Any]:
        ready, detail = self._direct_channel_probe(account)
        if ready:
            return {"state": SessionState.READY.value, "ready": True, "detail": detail}
        try:
            pids = L._target_pids(self._desktop_recipe.processes)
            if "TC.exe" not in pids.values():
                return {
                    "state": SessionState.LOGIN_REQUIRED.value,
                    "ready": False,
                    "detail": "实验交易内核 TC.exe 未启动；可调用登录接口",
                }
            dialog = L.find_login_dialog(pids)
            if dialog is not None:
                kind = L.classify(L.snapshot(dialog))
                if kind == "trade":
                    return {
                        "state": SessionState.LOGIN_REQUIRED.value,
                        "ready": False,
                        "detail": "已识别到实验 TC 交易登录窗口；可调用登录接口",
                    }
                return {
                    "state": SessionState.MANUAL_ACTION_REQUIRED.value,
                    "ready": False,
                    "detail": f"检测到尚未识别的实验 TC 窗口类型：{kind}",
                }
        except Exception as exc:  # noqa: BLE001 — 只读状态失败不能猜成未登录
            return {
                "state": SessionState.CHANNEL_UNAVAILABLE.value,
                "ready": False,
                "detail": f"检查实验 TC 登录窗口失败：{type(exc).__name__}",
            }
        return {
            "state": SessionState.CHANNEL_UNAVAILABLE.value,
            "ready": False,
            "detail": detail,
        }

    def close(self) -> None:
        """停止由本驱动托管的 HQMP 监听；普通通道无需处理。"""
        if self._hqmp_session is not None:
            self._hqmp_session.stop()

    # ── 页面补丁 ─────────────────────────────────────────
    def autoconfirm_status(self, *, account: str = "",
                           need_times: Sequence[Tuple[int, int]] = ()) -> Dict[str, Any]:
        """补丁那一项单独可查（诊断页要单独刷它，不必连客户端）。"""
        c = H.check_autoconfirm(account_no=account, need_times=need_times)
        return {"ok": c.ok, "warn": c.warn, "detail": c.detail}

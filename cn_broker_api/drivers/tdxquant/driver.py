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
from cn_broker_api.drivers.desktop_recipe import DesktopRecipe
from cn_broker_api.drivers.driver_error import DriverError
from cn_broker_api.drivers.ensure_result import EnsureResult
from cn_broker_api.drivers.tdxquant import health as H
from cn_broker_api.drivers.tdxquant import login as L
from cn_broker_api.drivers.tdxquant.client import TdxQuantClient, set_pyplugins
from cn_broker_api.drivers.tdxquant.mcp import McpClient
from cn_broker_api.drivers.tdxquant.trading import TdxQuantTrading
from cn_broker_api.state import PasswordVault, SubmitLatch

logger = logging.getLogger(__name__)

#: 这一版客户端的桌面配方。**数据，不是代码**（见 `drivers/base.py` 那段）：
#: 换一个更精简的客户端版本，多半是照着写一份新的配方，而不是改机制。
#:
#: ⭐ `processes` 的顺序就是**拉起顺序**：主程序先起（本地那个 JSON-RPC 端口是它开的），
#: 交易模块后起。
#: ⭐⭐ 交易模块**不会自己起来**：主程序开了自动登录之后，启动只登行情，交易那一半要人在
#: 界面上点一下【交易】才拉起 ⇒ 于是"既没登上、也没有登录框"这种谁都不动的僵局
#: （实测等 120 秒也没弹）。所以它必须列在这里，由我们自己拉。
TDX_RECIPE = DesktopRecipe(
    processes=("Tdxw.exe", "TC.exe"),
    executables={"Tdxw.exe": "Tdxw.exe", "TC.exe": str(Path("NewTc") / "TC.exe")},
)


class TdxQuantDriver:
    """真驱动。**Windows 专属**——非 Windows 上 import 得动，真去调才失败
    （在能跑的地方跑，在不能跑的地方明确失败，而不是 import 期就把服务拖挂）。"""

    name = "tdxquant"

    def __init__(self, cfg: TdxQuantConfig, *, latch: SubmitLatch,
                 vault: Optional[PasswordVault] = None) -> None:
        self.cfg = cfg
        self.latch = latch
        self.vault = vault or PasswordVault()
        # 把「唯一出处」注入进那两份搬来的模块：安装根目录、MCP 地址、凭据路径。
        # ⭐ 注入而不是让它们各自读配置——两处各推一份的话，换客户端时改一处、
        #    另一处静默指向不存在的目录，而「文件不在」会被自检读成「补丁没打」。
        H.set_tdx_home(cfg.tdx_home)
        set_pyplugins((cfg.tdx_home / "PYPlugins") if cfg.tdx_home else None)
        L.set_mcp_url(cfg.mcp_url)
        L.set_cred_path(cfg.cred_file if cfg.cred_source == "file" else None)

    # ── 能力 ─────────────────────────────────────────────
    def capabilities(self) -> List[str]:
        return [Capability.CREDIT_ORDER, Capability.CANCEL, Capability.BID_ASK_QUOTE,
                Capability.SELLABLE_VOLUME, Capability.DESKTOP_LOGIN,
                Capability.AUTOCONFIRM_PATCH]

    def client(self) -> McpClient:
        """自检那一侧的最小通道（只回答"通道通不通"，三十行）。"""
        return McpClient(self.cfg.mcp_url)

    def trading(self, *, account: str = "", account_type: str = "STOCK") -> TdxQuantTrading:
        """这个账户上的交易与查询。

        ⭐ 现构现用：底下那条连接是**进程级共享**的，`connect()` 命中同一个身份就短路，
        所以这里没有要缓存的东西。身份含账号与类别（`ConnKey`），换账户才会真重连。
        """
        client = TdxQuantClient(
            str(self.cfg.tdx_home / "PYPlugins") if self.cfg.tdx_home else None,
            account=account, account_type=account_type,
            transport=self.cfg.transport, mcp_url=self.cfg.mcp_url)
        return TdxQuantTrading(client, cancel_timeout=self.cfg.cancel_confirm_timeout,
                               cancel_interval=self.cfg.cancel_confirm_interval)

    # ── 桌面进程（看门狗要的两件事）─────────────────────
    def desktop_recipe(self) -> DesktopRecipe:
        return TDX_RECIPE

    def desktop_processes(self) -> Dict[str, bool]:
        """配方里那几个进程各自在不在跑。**只读、零成本**，不连客户端、不抢锁。

        ⭐ 「进程在跑」离「能下单」还有三道门 ⇒ 这个结果不能当成通道可用。
        """
        running = {name.lower() for name in
                   L.running_processes(TDX_RECIPE.processes).values()}
        return {n: (n.lower() in running) for n in TDX_RECIPE.processes}

    def start_desktop_process(self, name: str) -> None:
        """按配方拉起一个进程。**只拉起，绝不 kill**（见 `WatchdogConfig` 那段）。"""
        rel = TDX_RECIPE.executables.get(name)
        if not rel:
            raise DriverError(f"配方里没有 {name!r}，认得的是 {sorted(TDX_RECIPE.executables)}")
        try:
            L._spawn(rel)
        except SystemExit as e:                # _spawn 找不到 exe 时 SystemExit(2)
            raise DriverError(f"起 {name} 失败（{rel} 找不到？）：{e}") from e

    # ── 自检 ─────────────────────────────────────────────
    def health(self, *, account: str = "", account_type: str = "STOCK",
               need_times: Sequence[Tuple[int, int]] = ()) -> Dict[str, Any]:
        h = H.check_trade_channel(self.client(), account=account,
                                  account_type=account_type, need_times=need_times,
                                  probe_symbol=H.DEFAULT_PROBE_SYMBOL)
        return {
            "ok": h.ok,
            "message": h.message(),
            "checks": [{"key": c.key, "name": c.name, "ok": c.ok,
                        "warn": c.warn, "detail": c.detail} for c in h.checks],
        }

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
        cred = self._resolve_cred(password=password, account=account,
                                  account_type=account_type)
        acc = str(cred.get("account") or "")

        # ⭐⭐ 先看「是不是已经好了」，好了就直接返回——**不占用密码额度**。
        # 这一步就是「状态驱动而不是弹框驱动」在编排层的体现：真登录只在需要时发生。
        ok, detail = L.channel_ok(cred)
        if ok:
            return EnsureResult(ok=True, acted=False, detail=detail)

        # 要走到填密码那一步了，先要额度。**先记后点**（见 state 模块 docstring）。
        self.latch.claim(acc)
        try:
            ok, detail = L.ensure_logged_in(
                cred, wait=wait_seconds, start=start, minimize=minimize)
        except SystemExit as e:        # login._spawn 找不到 exe 时会 SystemExit(2)
            # 🔴 **刻意不结算**：`claim()` 已经先按失败记过，这里让它留着。
            #    起不来客户端与密码错在这一层分不开，而保守那一边的代价小得多。
            raise DriverError(f"起客户端失败：{e}") from e
        # ⭐ 必须结算：不调的表现是「登录成功了，但连续失败计数一直涨」，
        #    几天之后那个闸会把自己关死。
        self.latch.settle(acc, ok)
        return EnsureResult(ok=ok, detail=detail, acted=True)

    # ── 页面补丁 ─────────────────────────────────────────
    def autoconfirm_status(self, *, account: str = "",
                           need_times: Sequence[Tuple[int, int]] = ()) -> Dict[str, Any]:
        """补丁那一项单独可查（诊断页要单独刷它，不必连客户端）。"""
        c = H.check_autoconfirm(account_no=account, need_times=need_times)
        return {"ok": c.ok, "warn": c.warn, "detail": c.detail}

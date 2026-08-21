"""桌面客户端驱动：把搬来的那三份（登录 / 自检 / 页面补丁）装成一个驱动。

这一层**只做编排**，不含任何判据——判据全在 `login.py` / `health.py` 里，那两份是从生产
环境整文件搬来的，动它们等于把真机验证的结果作废。

## 密码从哪来

两条路，由配置的 `cred_source` 选：

- `file`    ：`login.load_cred()` 读仓库外的那个 JSON（搬迁期，行为与搬迁前逐字节相同）
- `request` ：调用方在请求里带过来，只在内存留当天一份（`state.PasswordVault`）

⭐ 做成一个 `_resolve_cred()` 接缝而不是两套代码：切换靠配置，两条路共用后面的一整段。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cn_broker_api.config import TdxQuantConfig
from cn_broker_api.drivers.base import Capability, DriverError, EnsureResult
from cn_broker_api.drivers.tdxquant import health as H
from cn_broker_api.drivers.tdxquant import login as L
from cn_broker_api.drivers.tdxquant.mcp import McpClient
from cn_broker_api.state import PasswordVault, SubmitLatch

logger = logging.getLogger(__name__)


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
        L.set_mcp_url(cfg.mcp_url)
        L.set_cred_path(cfg.cred_file if cfg.cred_source == "file" else None)

    # ── 能力 ─────────────────────────────────────────────
    def capabilities(self) -> List[str]:
        return [Capability.CREDIT_ORDER, Capability.CANCEL, Capability.BID_ASK_QUOTE,
                Capability.SELLABLE_VOLUME, Capability.DESKTOP_LOGIN,
                Capability.AUTOCONFIRM_PATCH]

    def client(self) -> McpClient:
        return McpClient(self.cfg.mcp_url)

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
            raise DriverError(f"起客户端失败：{e}") from e
        return EnsureResult(ok=ok, detail=detail, acted=True)

    # ── 页面补丁 ─────────────────────────────────────────
    def autoconfirm_status(self, *, account: str = "",
                           need_times: Sequence[Tuple[int, int]] = ()) -> Dict[str, Any]:
        """补丁那一项单独可查（诊断页要单独刷它，不必连客户端）。"""
        c = H.check_autoconfirm(account_no=account, need_times=need_times)
        return {"ok": c.ok, "warn": c.warn, "detail": c.detail}

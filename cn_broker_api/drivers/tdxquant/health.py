"""交易通道可用性自检（从母项目 `backend/gateway/cn/channel_health.py` 整文件搬来）。

只改了接缝，判据一个字符没动——尤其 `check_quote`，那段 2026-08-21 刚按实盘修过并做过负向
验证，retype 一遍等于把验证结果作废。接缝三处：logger 换标准库；安装根目录从本服务的 config
来；`check_account` / `check_trade_channel` 从吃 gateway 改成吃 `McpClient` + 账号。

它把「今天能不能真的报出单」拆成四个**可分辨**的问题，全部价值就在于**分开报**：
处置完全不同（去开客户端 / 去登录交易 / 去看行情连接 / 去重打补丁），混成一句
「通道不可用」等于没说。

为什么这四项：自动确认补丁失效的方向虽然安全（回到人工点），但盘中失效的代价不对称——
买入腿不成交只是少赚，14:57 还款腿不成交就是负债过夜，而它唯一的现象是"没人点就没成交"，
是最需要主动报警的那类静默失效（客户端升级会把 `aireq.html` 冲掉）。另外三个前提各自独立，
任何一个不成立都表现为"到点了什么都没发生"：客户端进程在不在（MCP 端口是它开的）、
交易账号登录了没、客户端行情通不通——报单会被「当前客户端行情处于断开状态」挡住，
而**查询那时全是绿的**（2026-08 实测，四小时没人发现）。

本模块只观测，不下单、不改文件，也**不阻断**调用方：报单该发还是发（信号躺在队列里等人点，
比不发好），自检的产出是一条 CRITICAL 事件。

每一项的能力边界写在各自的 docstring 里。尤其行情那项：它挡得住"行情侧完全没数"，挡不住
"缓存里有数而连接已断"——真正的判据只有第一笔委托的回执，所以它给的是 warn 不是保证。
"""
from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import logging

from cn_broker_api.drivers.tdxquant.mcp import McpClient, has_money_fields

logger = logging.getLogger(__name__)

#: 客户端安装根目录。**本服务里唯一的出处**——由入口在起服务时按配置注入一次。
#: 刻意做成模块级可注入而不是每次读配置：两处各推一份的话，换客户端时改一处、
#: 另一处静默指向不存在的目录，而「文件不在」会被自检读成「补丁没打」这种假警报。
_TDX_HOME: Optional[Path] = None


def set_tdx_home(path: Optional[Path]) -> None:
    global _TDX_HOME
    _TDX_HOME = Path(path) if path else None


def tdx_install_root() -> Path:
    """客户端装在哪。没配就明确失败——**不猜**：猜错的表现是"补丁没打"这种假警报。"""
    if _TDX_HOME is None:
        raise RuntimeError(
            "没配客户端安装目录（config.toml 里的 driver.tdxquant.tdx_home）⇒ "
            "自动确认补丁那一项无从检查")
    return _TDX_HOME

#: 自动确认补丁的两个文件（页面本体 + 旁挂配置）。路径从安装根目录推，见 `tdx_install_root`。
SIDECAR_NAME = "tq_autosend.js"

#: 行情探针默认用的票：**要挑最活跃的**（平安银行）。冷门票在集合竞价刚开始时盘口可能全空，
#: 那会把"客户端行情没问题"读成故障——探针的假警报比不探更糟，人会学着忽略它。
DEFAULT_PROBE_SYMBOL = "000001.SZ"


def autoconfirm_paths() -> Tuple[Path, Path]:
    """(页面, 旁挂配置) 的绝对路径。"""
    cfg = tdx_install_root() / "webs" / "cfg"
    return cfg / "aireq.html", cfg / SIDECAR_NAME


@dataclass(frozen=True)
class Check:
    """一项检查的结论。`ok=False` 才是坏；`warn` 是"没能判实"，不算坏。"""

    key: str
    name: str
    ok: bool
    detail: str = ""
    warn: bool = False

    def line(self) -> str:
        mark = "" if (self.ok and not self.warn) else ("" if self.ok else "")
        return f"{mark} {self.name}：{self.detail}" if self.detail else f"{mark} {self.name}"


@dataclass
class ChannelHealth:
    """四项检查的汇总。"""

    checks: List[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failed(self) -> List[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def warned(self) -> List[Check]:
        return [c for c in self.checks if c.ok and c.warn]

    def critical(self) -> List[str]:
        """给 CRITICAL 事件用的行。**只有真失败进去**，warn 不进——把"没能判实"也报成告警，
        人会很快学会无视整条告警（同 `_t0_alert` 里"普通 errors 不造事件"的口径）。"""
        return [f"{c.name}：{c.detail}" for c in self.failed]

    def message(self) -> str:
        head = "通道自检通过" if self.ok else f"通道自检**不通过**（{len(self.failed)} 项）"
        return head + "｜" + "；".join(c.line() for c in self.checks)


# ────────────────────────── 四项检查 ──────────────────────────

def check_transport(client) -> Check:
    """① 客户端进程在不在。

    mcp 通道下这是一个**独立于业务调用**的端口探针：`127.0.0.1:17709` 是客户端进程
    （`Tdxw.exe`）自己开的，端口不通就一定是客户端没开/量化模块没加载，与账号登录无关。
    ctypes 通道没有等价的便宜探针（IPC 建连本身就是那次业务调用），故只报 warn 并把判断
    留给下一项——**不假装自己检查过**。
    """
    if getattr(client, "transport", "") != "mcp":
        return Check("transport", "客户端进程", True,
                     f"通道 {getattr(client, 'transport', '?')}，无独立端口探针，"
                     f"连不上会体现在下一项", warn=True)
    url = getattr(client, "mcp_url", "") or ""
    u = urlparse(url)
    host, port = u.hostname or "127.0.0.1", u.port or 80
    try:
        with socket.create_connection((host, port), timeout=2.0):
            return Check("transport", "客户端进程", True, f"{host}:{port} 通")
    except OSError as e:
        return Check("transport", "客户端进程", False,
                     f"{host}:{port} 不通（{type(e).__name__}）⇒ 通达信客户端没开，"
                     f"或开的那个版本不带量化模块。今天一笔都发不出去")


def check_account(client: McpClient, *, account: str = "",
                  account_type: str = "STOCK") -> Check:
    """② 交易账号登录了没（要一次句柄 + 一次真实的资产查询）。

    句柄拿到了**不等于**登录了交易：实测有 `ErrorId=0` 而 `Value:[]` 的形态。所以这里判的是
    资产里**有没有资金字段**——「取不到」与「权益是 0」是两件事，前者该报查询失败，
    后者会被当成真实数字（探活显示 0、调仓预算算出 0 手）。

     账号必须显式填：「默认账户」解析成谁随登录状态变，不填就可能查到另一个账户。
    """
    try:
        handle = client.stock_account(account=account, account_type=account_type)
        if handle is None:
            return Check("account", "交易账号登录", False,
                         f"要不到账户句柄（{client.last_error or '厂商未给原因'}）"
                         f"⇒ 客户端里交易没登录，或账号与类别不是一对")
        asset = client.query_stock_asset(handle)
    except Exception as e:  # noqa: BLE001 — 任何异常都是"这一项不通"，不该弄死自检
        return Check("account", "交易账号登录", False,
                     f"查资产失败：{type(e).__name__}: {str(e)[:160]}")
    if not client.ok(asset) or not has_money_fields(asset):
        return Check("account", "交易账号登录", False,
                     "查得到账户但没有任何资金字段（厂商的『成功但没内容』形态）"
                     "⇒ 客户端里交易没登录，或账号与类别不是一对")

    def _f(key: str) -> float:
        try:
            return float(str(asset.get(key, 0)).strip() or 0)
        except (TypeError, ValueError):
            return 0.0

    return Check("account", "交易账号登录", True,
                 f"权益 {_f('Asset'):.2f} / 可用 {_f('Cash'):.2f}")


#: 连续竞价时段（本地时钟，两段）。**只在这两段里**「现价为 0」才算行情侧出问题；
#: 其余时刻（盘前、午休、收盘后）没有现价是正常状态，判红就是每天早上一次假警报。
CONTINUOUS_SESSIONS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))


def _in_continuous_session(now: time) -> bool:
    return any(lo <= now < hi for lo, hi in CONTINUOUS_SESSIONS)


def check_quote(client, *, probe_symbol: str = DEFAULT_PROBE_SYMBOL,
                now: Optional[datetime] = None) -> Check:
    """③ 客户端自己那条行情通不通。

     必须走 `client.query_snapshot`（客户端的行情侧），**不能**用项目里别处的行情
    （`sources/tdx.py` 是自研 socket 直连公网服务器，与客户端毫无关系，客户端断了它照样绿）。

     **判的是「行情侧答不答话」，不是「有没有现价」**（2026-08-21 修正）。证据是快照里
    有一条认得出的记录（昨收有数）；现价则取决于时点——集合竞价还没撮合时它本来就是 0。
    此前把"现价为 0"一律判红，结果 09:15 自检每天报一次假警报：那天报红，而 09:20 报单
    两只全部报出、09:26 全部成交。**假警报比不探更糟**，人会学着无视整条自检。

    ⇒ 「现价为 0」只在 `CONTINUOUS_SESSIONS` 里判红（那时候没有现价确实是行情侧断了），
    盘前/午休/收盘后照常通过，detail 里说明现价还没有。

     **能力边界**（不变）：客户端断线后本地缓存仍可能给出上一笔盘口，那种情况这一项会
    通过而报单照样被挡 ⇒ 通过只当"没发现问题"，不当"能报单"的保证。真正的判据只有第一笔
    委托的回执。快照里没有时间戳字段（实测），所以判不了"这个数是什么时候的"。
    """
    try:
        snap = client.query_snapshot(probe_symbol)
    except Exception as e:  # noqa: BLE001
        return Check("quote", "客户端行情", False,
                     f"取 {probe_symbol} 快照失败：{type(e).__name__}: {str(e)[:160]}")
    if not snap:
        return Check("quote", "客户端行情", False,
                     f"取不到 {probe_symbol} 的快照（ErrorId 非 0）⇒ 客户端行情侧不通，"
                     f"报单会被「当前客户端行情处于断开状态」挡住")

    def _f(key: str) -> float:
        v = snap.get(key)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else 0
        try:
            return float(str(v).strip() or 0)
        except (TypeError, ValueError):
            return 0.0

    px, bid1, ask1 = _f("Now"), _f("Buyp"), _f("Sellp")
    prev_close = _f("LastClose")
    at = (now or datetime.now()).time()
    live = _in_continuous_session(at)

    # 记录回来了却连昨收都没有＝答了话但整条记录是空的，这才是"行情侧没数"。
    if prev_close <= 0 and px <= 0:
        return Check("quote", "客户端行情", False,
                     f"{probe_symbol} 快照回来了但现价与昨收都是 0 ⇒ 行情侧没数，"
                     f"报单可能被「当前客户端行情处于断开状态」挡住")
    if px <= 0:
        if live:
            return Check("quote", "客户端行情", False,
                         f"{probe_symbol} 现价为 0（昨收 {prev_close}），而现在 {at:%H:%M} "
                         f"在连续竞价时段内 ⇒ 行情侧断了")
        return Check("quote", "客户端行情", True,
                     f"{probe_symbol} 昨收 {prev_close}、现价还没有（{at:%H:%M} 未开盘）"
                     f"⇒ 行情侧在答话")
    if live and bid1 <= 0 and ask1 <= 0:
        # 只在两侧都空时说话且只 warn：封板时本来就只有一侧，判成故障就是假警报。
        # 盘前不进这一格，那时盘口空是常态。
        return Check("quote", "客户端行情", True,
                     f"{probe_symbol} 现价 {px} 有数但买卖盘口全空（封板？）", warn=True)
    return Check("quote", "客户端行情", True,
                 f"{probe_symbol} 现价 {px}（买一 {bid1} / 卖一 {ask1}）")


def check_autoconfirm(*, account_no: str = "",
                      need_times: Sequence[Tuple[int, int]] = ()) -> Check:
    """④ 自动确认补丁在不在、配置对不对（纯读文件，不连客户端）。

    问的是**功能性**问题而不是"有没有那个标记注释"：页面里有没有引到旁挂配置
    (`tq_autosend.js`)、配置里 `enabled` 真不真、档位是不是生产档、资金账号对不对、
    以及 `hours` **覆盖不覆盖今天真的要报单的那几个时刻**。

     最后那一项是这里唯一"补丁在也可能不管用"的格子：`hours` 少覆盖 14:57，
    还款那笔就会静默地躺在队列里等人点——而页面上一切正常、徽标也是绿的。

     刻意**不比对补丁的标记注释**（`<!-- TQ-AUTOSEND-BEGIN ... -->`）：那种魔法字符串一旦
    要在两个文件里同步，就一定会有一天不同步，而不同步的表现是"自检说没打补丁"这种假警报。
    引用的文件名是功能本身要求的，不会为了改版而变。
    """
    page, sidecar = autoconfirm_paths()
    if not page.exists():
        return Check("autoconfirm", "自动确认补丁", False,
                     f"找不到页面 {page}（客户端目录变了？CN_TDX_HOME 没设对？）")
    try:
        html = page.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return Check("autoconfirm", "自动确认补丁", False, f"页面读不出来：{e}")
    if SIDECAR_NAME not in html:
        return Check("autoconfirm", "自动确认补丁", False,
                     f"页面里没有引到 {SIDECAR_NAME} ⇒ 补丁不在（客户端升级冲掉了？）。"
                     f"今天每一笔委托都要人在「交易信号」窗口里点【发送】，"
                     f"**14:57 还款那笔漏点＝负债过夜**")
    if not sidecar.exists():
        return Check("autoconfirm", "自动确认补丁", False,
                     f"补丁在，但配置 {sidecar} 不在 ⇒ 自动确认是关的（fail closed 的设计），"
                     f"每笔都要人点")
    try:
        txt = sidecar.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"window\.__TQ_AUTOSEND\s*=\s*(\{.*?\})\s*;", txt, re.S)
        cfg: Dict[str, Any] = json.loads(m.group(1)) if m else {}
    except (OSError, ValueError) as e:
        return Check("autoconfirm", "自动确认补丁", False, f"配置解析失败：{str(e)[:160]}")

    bad: List[str] = []
    if not cfg.get("enabled"):
        bad.append("配置里 enabled 不为真 ⇒ 关着")
    mode = str(cfg.get("mode") or "send")
    if mode != "send":
        bad.append(f"档位是 {mode}（演练档会把信号**取消**掉，不是发送）")
    want = (account_no or "").strip()
    have = str(cfg.get("account") or "").strip()
    if want and have and want != have:
        bad.append(f"配置里的资金账号 {have} 与本实例账户 {want} 不同 ⇒ 我们的信号会被跳过")
    miss = [f"{h:02d}:{m2:02d}" for h, m2 in need_times
            if not _covered(cfg.get("hours") or [], h, m2)]
    if miss:
        bad.append(f"hours 没覆盖 {', '.join(miss)} ⇒ 那几个时刻的委托要人工点")
    detail = (f"{page.name} 已引 {SIDECAR_NAME}；档位 {mode}、账号 {have or '(不限)'}、"
              f"单笔上限 {cfg.get('maxVol')} 股 / {cfg.get('maxNotional')} 元、"
              f"行龄 ≤{cfg.get('maxAgeSec')}s")
    if bad:
        return Check("autoconfirm", "自动确认补丁", False, "；".join(bad) + f"（{detail}）")
    return Check("autoconfirm", "自动确认补丁", True, detail)


def _covered(hours, hh: int, mm: int) -> bool:
    """`hours` 覆盖 hh:mm 吗。区间**两端都算在内**，与注入脚本里那段 `inHours` 逐字一致
    （`t >= a && t <= b`）——判据不同的话，自检说覆盖而页面不发，是最难查的一种不一致。"""
    t = hh * 60 + mm
    for span in hours or []:
        try:
            a, b = str(span[0]), str(span[1])
            ah, am = (int(x) for x in a.split(":")[:2])
            bh, bm = (int(x) for x in b.split(":")[:2])
        except (TypeError, ValueError, IndexError):
            continue
        if ah * 60 + am <= t <= bh * 60 + bm:
            return True
    return False


# ────────────────────────── 编排 ──────────────────────────

def check_trade_channel(client: McpClient, *, account: str = "",
                        account_type: str = "STOCK",
                        need_times: Sequence[Tuple[int, int]] = (),
                        probe_symbol: str = DEFAULT_PROBE_SYMBOL,
                        now: Optional[datetime] = None) -> ChannelHealth:
    """四项一起跑，返回汇总。**任何一项抛异常都不该让自检整体失败**（它自己就是兜底件）。

     ① 与 ④ 刻意**在连接之外**做：端口探针与文件检查不需要说上话，所以"客户端根本没开"
    这一格仍然能报出**它自己的**那句话，而不是被连接异常盖掉。

     母项目那份有个 `session` 参数（账户会话锁）。本服务不需要：跨账户的串行由本服务
    内部的亲和排队负责，锁不该出现在自检的签名里。
    """
    out = ChannelHealth([check_transport(client)])
    try:
        out.checks.append(check_account(client, account=account, account_type=account_type))
        out.checks.append(check_quote(client, probe_symbol=probe_symbol, now=now))
    except Exception as e:  # noqa: BLE001 — 连不上就是"这两项没能做"，如实说
        out.checks.append(Check("account", "交易账号登录", False,
                                f"连接失败：{type(e).__name__}: {str(e)[:200]}"))
        out.checks.append(Check("quote", "客户端行情", True,
                                "连接没建起来，这一项没做", warn=True))
    out.checks.append(check_autoconfirm(account_no=account, need_times=need_times))
    logger.info("[health] %s", out.message())
    return out

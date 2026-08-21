"""TdxQuant 低层客户端：封装通达信官方量化模块 tqcenter 的连接与调用。

tqcenter 随通达信量化版安装在 `<install>\\PYPlugins\\{user,sys}`，加载 64 位 TPythClient.dll
经 IPC 连**本地已登录的通达信客户端**下单/查询——**账号密码留在客户端，无第三方黑盒 DLL**（官方、
非逆向）。与 miniQMT/xtquant 同形（order_stock / query_stock_*），但用通达信生态、64 位免桥。

懒加载：只有 `connect()` 才 `import tqcenter`，故无通达信环境的机器仍能 import 本模块（供单测/CI）。
前提：64 位 Python + 通达信金融终端(量化模拟/正式)已启动并登录交易。

## 账户参数是**被校验**的，可当强校验用（实测）

`stock_account(account=..., account_type=...)` 的两个入参 tqcenter **都会校验**，实测：

| 传入 | 结果 |
| --- | --- |
| `account=""`（现所有调用点都这样） | 通过，句柄 `1` ＝ 默认账户 |
| `account="999999999"` | **拒绝**：「资金账户未登录或不存在。」 |
| `account_type="NO_SUCH_TYPE"` | **拒绝**：「account_type error:NO_SUCH_TYPE」 |

⇒ **配置里写全资金账号就能得到强校验**：连上了必定是那个账户，配错直接连不上，
不存在"静默连到别的账户去下单"。这比事后拿资金/持仓做指纹比对可靠得多。
多账户抽象落地时应当要求显式填账号，把这条用起来（现在的空串＝默认账户，无校验）。

### ⚠️ `account_type` 与资金账号是**一对**，不匹配的表现是「账户不存在」（实测）

`account_type` 的合法取值是 `TPyth.dll` 里一张固定枚举表（偏移 `0x8d288` 起四个连续字符串）：
**`OPTION` / `FUTURE` / `CREDIT` / `STOCK`**。它决定去哪张账户名单里查 `account`：

| account | account_type | 结果 |
| --- | --- | --- |
| 12795515（普通模拟） | `STOCK` | 通过，句柄 1 |
| 1210019487（信用模拟） | `STOCK` | **拒绝**：「资金账户未登录或不存在。」 |
| 1210019487（信用模拟） | `CREDIT` | 通过，句柄 **2** |
| ""（默认） | `CREDIT` | 通过，句柄 2（＝该类别下的默认账户） |

⇒ **信用（两融）账户必须给 `CREDIT`**。踩这个坑时报错文案说的是"客户端未登录"，
与真实原因（类别填错）差得很远，所以下面 `connect()` 的失败文案把类别一并报出来。
本网关只支持 `STOCK` / `CREDIT` 两类（tqcenter 的下单查询接口都是 `*_stock_*`，
期货/期权那两类没有对应的委托类型映射，见 `tdxquant_gateway` 的 `CnCreditOrderKind`）。

⚠️ 另外 `account_id` 返回的是**句柄/序号**（这里是 1），不是资金账号；
`query_asset()` / 持仓行 / 委托行**都不带任何账户身份信息**，所以除了上面这条，
系统没有别的办法知道自己连的是谁。

## 连接是**进程级单例**，不是每个实例一份

`tqcenter.tq` 是个**只有类方法、状态全在类属性上**的类（`tq._initialized` / `tq._reInitialized`
/ `tq.run_id`），`dll = ctypes.CDLL(...)` 也在模块顶层。也就是说：一个进程里无论 new 出多少个
`TdxQuantClient`，底下**只有一条连接**——"每次操作现构现连、用完即断"那种写法是个假象。

### "连不上必须重启后端"的真实机制（真机逐条跑过，不是推断）

| 操作序列 | 结果 |
|---|---|
| `close()` → `initialize()` | **正常**，0.025s |
| `_reInitialize()` → `initialize()`（连接仍开着） | **正常**，3.0s（这是厂商设计的接管路径） |
| `close()` → `_reInitialize()` → `initialize()` | **失败**「与通达信客户端建立连接失败」，15~22s |
| 上一格之后再把 `_reInitialized` 置回 False 重试 | **仍然失败**——损坏在 DLL 侧，改 Python 标志救不回来 |
| 对一条**已经断掉**的会话再 `close()` 一次 | **无限挂住**（比它要修的 15 秒失败更糟） |

`_reInitialized` 是 `dll.InitConnect(..., cls._reInitialized)` 的最后一个参数，含义是
「**接管已存在的会话**」。已经 `CloseConnect` 掉之后没有会话可接管 ⇒ InitConnect 失败；
而该标志**只在成功时才清零** ⇒ 之后每次都带着它去接管一个不存在的会话 ⇒ 永久卡死，只能重启进程。

而 tqcenter **自己**会在 `stock_account` 的 `ErrorId in ["6","7"]` 分支和异常分支里调
`_reInitialize()`。所以一旦交易掉线/客户端重启过，接管失败一次，这个标志就再也不会被清掉。
每次重试的 15 秒来自 `GetOrderStr` 的 `timeout_ms = 10000` 加上失败的 InitConnect。

⇒ **三条铁律**（都是踩出来的，见 `hard_reset` 的注释）：
  1. **绝不主动调 `_reInitialize()`**——`close()` 之后调它就是毒药。
  2. 每次重连前把 `_reInitialized` **清零**，让 `InitConnect` 走"新连接"而不是"接管"。
  3. **只在 tqcenter 认为还连着时才 `close()`**。重复断会挂死，所以既不猜也不还原 run_id。

第二个代价是 **churn**：实测 `close()` 恒 0.5 秒，而一次调仓要连三四次（预览取权益、执行、
刷持仓、刷委托各一次），且调度器与人点按钮会撞在同一个策略名上（InitConnect 报的
"已有同名策略运行"就是它）。连接次数越多，踩上上面那个雷的机会越多。

于是本模块的形态是：**进程内共享一条长连接 + 一把锁**。
  - `connect()` 命中健康的共享连接就直接复用（实测 0.006~0.008s），不必每次 InitConnect。
  - `close()` **默认不真断**（软释放），避免 churn；真断走 `hard_reset()`。
  - 失败路径一律先 `hard_reset()` 再重建，**让下一次点击真的能重连**，不必重启后端。
  - 所有 tqcenter 调用都在 `_LOCK` 里串行：类级全局状态 + DLL 都不是线程安全的，而 waitress
    是多线程、调度器又是另一个线程。

## 两条通道：`mcp`（默认）与 `ctypes`（上面那一整套）

上面描述的全是 **ctypes 通道**：本进程 `ctypes.CDLL(TPythClient.dll)` 经 IPC 连客户端。
2026-08-17 换到「通达信金融终端(开心果整合版)V2026X64」之后，这条路**连不上**——
`import tqcenter` 成功（DLL 加载没问题），但 `tq.initialize()` 报
「初始化错误：请确认是否打开通达信客户端 / 连接路径为空」，即 IPC 通道压根没建起来。
而同一时刻 `TPyth.dll` 自带的 **MCP over HTTP**（`127.0.0.1:17709`）完全正常。

于是本模块支持两条通道，由 `CN_TDXQUANT_TRANSPORT` 选（`mcp` / `ctypes`）：

| | ctypes | mcp |
| --- | --- | --- |
| 走法 | 本进程加载 DLL，IPC | JSON-RPC over HTTP，方法名＝tqcenter 函数名 |
| 进程级单连接的雷 | 全都在（`_reInitialized` 卡死、重复 close 挂住） | **不存在**，HTTP 无状态 |
| 位数约束 | 要 64 位 Python | 无（跨进程，甚至可跨机器） |
| 当前整合版客户端 | ❌ 连不上 | ✅ 通 |

⭐ **两条通道共用同一份编号表**：`tqconst` 是纯 Python 常量（`PRICE_MY=0` / `STOCK_BUY=0` /
`CREDIT_FIN_BUY=69` / `CREDIT_STK_REPAY=76` …），`prepare_import()` 之后 `import` 就能拿到，
**不需要 DLL 初始化成功**——这正是 mcp 通道仍然要 PYPlugins 路径的唯一原因。编号只有
tqcenter 一份这条纪律没有被打破（项目里不抄第二份编号表）。

🔴 **不做自动回落**。两条通道的故障表现完全不同（ctypes 失败是"连不上"、mcp 失败是 HTTP 错），
自动回落会让"我以为走的是 A、其实走的是 B"，而这是**交易通道**——同一个操作在两条路上
可能一个成功一个失败，事后连查都没法查。要换就显式改环境变量。

⚠️ mcp 通道走 `core.net.direct_session()` 而不是裸 `requests`：本机系统代理在**注册表**里，
requests 默认 `trust_env=True` 会吃到它；`127.0.0.1` 靠系统代理的 `<local>` 例外躲过去只是
侥幸（`core/net.py` docstring 第③条讲的就是这件事），不能指望。
"""
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

logger = logging.getLogger(__name__)

# PYPlugins 路径：环境变量 CN_TDXQUANT_PYPLUGINS 覆盖；缺省本机在用的客户端（按机器改）。
# ⚠️ mcp 通道也需要它——不为连接，只为 `import tqconst` 拿编号表（见模块 docstring）。
#: ⚠️ 搬进本仓库时去掉了写死的本机路径（那是使用者机器上的目录，一个准备公开的仓库里
#: 不该有）。真值由驱动在构造时按配置的 `tdx_home` 注入 —— 见 `set_pyplugins()`。
#: 环境变量仍然认，那是临时覆盖用的。
_DEFAULT_PYPLUGINS = os.environ.get("CN_TDXQUANT_PYPLUGINS", "")

#: 通道选择，见模块 docstring 的对照表。**不自动回落**，配错就报错。
TDX_TRANSPORTS = ("mcp", "ctypes")
_DEFAULT_TRANSPORT = (os.environ.get("CN_TDXQUANT_TRANSPORT") or "mcp").strip().lower()

#: MCP over HTTP 的地址（`TPyth.dll` 自带，只绑本机）。跨机器时改这个。
_DEFAULT_MCP_URL = (os.environ.get("CN_TDXQUANT_MCP_URL")
                    or "http://127.0.0.1:17709").rstrip("/")

#: MCP 单次调用超时（秒）。查询侧对齐 tqcenter 自己的 `timeout_ms=10000`；下单给足余量——
#: 报单超时**不代表没报出去**（同 `_cancel_then_settle` 那条判据），超时越短这种两可状态越多。
_MCP_TIMEOUT_QUERY = 15.0
_MCP_TIMEOUT_ORDER = 30.0

#: tqcenter 认识的全部账户类别（`TPyth.dll` 里的枚举表，见模块 docstring）。仅供报错提示，
#: 不做白名单——真正的校验在 tqcenter 那边，这里多一层名单只会在厂商加类别时挡住人。
TDX_ACCOUNT_TYPES = ("STOCK", "CREDIT", "FUTURE", "OPTION")

#: 本网关支持的两类：普通（现金）账户与信用（两融）账户。
TDX_ACCOUNT_TYPES_SUPPORTED = ("STOCK", "CREDIT")

#: 串行化所有 tqcenter 调用。`tq` 的状态全在类属性上、DLL 也非线程安全，而调用方是多线程
#: （waitress 工作线程 + 定时任务线程）。可重入：`connect()` 里还会调 `query_asset` 做探活。
_LOCK = threading.RLock()

#: 进程级共享连接。`tq` 一进来就记下——**无论后面成不成**，`hard_reset()` 都得能真的断开；
#: 旧实现把它记在实例上且只在全部成功后才赋值，于是失败那次的全局状态没人能清（见模块 docstring）。
_SHARED: Dict[str, Any] = {"tq": None, "tqconst": None, "account_id": None, "key": None}


# ⚠️ 搬进本仓库时**删掉了这里的 `tdx_install_root()`**。它在母项目里是安装根目录的唯一
#    出处，但本仓库里那个角色已经由 `health.tdx_install_root()` 担着（入口按配置注入一次，
#    `autoconfirm` 与 `login` 都从它推）。留两份正好会犯它自己 docstring 里警告的那个错：
#    两处各写一份，换客户端时改一处、另一处静默指向不存在的目录。


def set_pyplugins(path: Optional[Path]) -> None:
    """注入 PYPlugins 目录（客户端安装目录下那一个）。

    ⭐ 与 `health.set_tdx_home()` / `login.set_mcp_url()` 同一个形状：**唯一出处由入口
    按配置注入一次**，模块自己不去读配置。搬进本仓库时那个写死的本机路径删掉了——
    一个准备公开的仓库里不该有使用者机器上的目录。
    """
    global _DEFAULT_PYPLUGINS
    _DEFAULT_PYPLUGINS = str(path) if path else ""


class ConnKey(NamedTuple):
    """共享连接的身份。**具名而不是裸元组**：它已经因为加字段碎过一次——测试按 `_key[1]`
    这样的下标断言，2026-08-17 在最前面插入 `transport`/`mcp_url` 之后三个用例齐刷刷地错位。
    具名之后再加字段，按名字取的地方一个都不会动。"""

    transport: str
    mcp_url: str
    pyplugins: str
    account: str
    account_type: str


class TdxQuantConnectionError(Exception):
    """连不上通达信量化（客户端未启动/未登录交易/路径错/位数不符/依赖缺失）。"""


class _McpTransport:
    """MCP over HTTP 通道。**方法名与参数名与 tqcenter 一字不差**，所以把它塞进
    `_SHARED["tq"]` 之后，上层那些 `tq.query_stock_asset(account_id=...)` 的调用点一行都不用改。

    与 ctypes 通道唯一的语义差异在**三态**：tqcenter 的查询函数直接返回 list，而 MCP 把它包在
    `{"ErrorId": "0", "Value": [...]}` 里 ⇒ 这里必须自己把「ErrorId 非 0」翻译成 `None`
    （判不了）而**不是** `[]`（真空仓）。🔴 翻错的后果见 `TdxQuantClient.query_positions`
    的注释：对账那侧会把持仓账本整表删掉、执行那侧会按从零建仓重复买入。
    """

    def __init__(self, base_url: str = _DEFAULT_MCP_URL):
        self.base_url = base_url
        #: 最近一次**业务失败**（ErrorId != 0）的厂商原文。连接失败的报错要带上它——
        #: 「资金账户未登录或不存在」这类话是排查的全部线索，吞掉就只剩一句我们自己的猜测。
        self.last_error: str = ""
        self._seq = 0
        self._session = None

    # ── 传输层 ───────────────────────────────────────────
    def _sess(self):
        """⚠️ 必须走 `direct_session()`：系统代理在注册表里，requests 默认会吃到它，
        而 `127.0.0.1` 靠 `<local>` 例外躲过去只是侥幸（`core/net.py` docstring 第③条）。"""
        if self._session is None:
            from cn_broker_api.net import direct_session

            self._session = direct_session()
        return self._session

    def _call(self, method: str, params: Dict[str, Any], *, timeout: float) -> Dict[str, Any]:
        """一次 JSON-RPC 调用，返回 `result` 字典。

        🔴 **「传输层失败」与「业务失败」必须分开**：前者（连不上、HTTP 非 2xx、协议错）抛
        异常，后者（`ErrorId != 0`）照常返回让调用方按各自语义判。混成一种，查询类就再也没法
        把「查不到」和「真的没有」分开——那正是这个模块里代价最大的一类错。
        """
        self._seq += 1
        payload = {"jsonrpc": "2.0", "id": self._seq, "method": method, "params": params}
        try:
            resp = self._sess().post(self.base_url, json=payload, timeout=timeout)
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:  # noqa: BLE001
            raise TdxQuantConnectionError(
                f"MCP 调用 {method} 失败（{self.base_url}）：{type(e).__name__}: {str(e)[:160]}"
                f"——确认通达信客户端已启动、量化模块已加载（该端口由 TPyth.dll 提供）") from e
        if isinstance(body, dict) and body.get("error"):
            err = body["error"] or {}
            raise TdxQuantConnectionError(
                f"MCP 拒绝 {method}：{err.get('message')}（code={err.get('code')}）"
                f"——方法名必须与 tqcenter 函数名一致")
        res = body.get("result") if isinstance(body, dict) else None
        if not isinstance(res, dict):
            raise TdxQuantConnectionError(f"MCP {method} 返回体不是对象：{str(body)[:200]}")
        return res

    @staticmethod
    def _ok(res: Dict[str, Any]) -> bool:
        return str(res.get("ErrorId", "0")) == "0"

    def _note_error(self, res: Dict[str, Any], method: str) -> None:
        self.last_error = str(res.get("Error") or res.get("Msg") or res)[:200]
        logger.warning("[tdx.mcp] %s 业务失败: %s", method, self.last_error)

    def _rows(self, res: Dict[str, Any], method: str) -> Optional[List[Dict[str, Any]]]:
        """查询类的三态映射。`ErrorId` 非 0 ⇒ None（判不了）；`Value` 不是数组也按 None
        （「成功但没内容」同样是判不了，不能读成真空仓）；否则原样给出，`[]` 才是真的没有。"""
        if not self._ok(res):
            self._note_error(res, method)
            return None
        value = res.get("Value")
        return list(value) if isinstance(value, list) else None

    # ── 以下方法名/参数名与 tqcenter 对齐，勿改 ──────────────
    def initialize(self, _path: str = "") -> None:
        """空操作：HTTP 没有"建立连接"这一步。留着是为了 `connect()` 两条通道共用一段代码。"""

    def stock_account(self, *, account: str = "", account_type: str = "STOCK"):
        res = self._call("stock_account",
                         {"account": account, "account_type": account_type},
                         timeout=_MCP_TIMEOUT_QUERY)
        if not self._ok(res):
            self._note_error(res, "stock_account")
            return None                      # 让上层走那段详细的"账号与类别是不是一对"文案
        value = res.get("Value")
        return int(value) if isinstance(value, (int, float)) else None

    def query_stock_asset(self, *, account_id):
        # 资产是**字段平铺在 result 上**（不像持仓/委托包在 Value 里），形状与 tqcenter 一致，
        # 所以整个给出去，ErrorId 的判断留在上层（`_probe` 就是看它）。
        return self._call("query_stock_asset", {"account_id": account_id},
                          timeout=_MCP_TIMEOUT_QUERY)

    def query_stock_positions(self, *, account_id):
        return self._rows(
            self._call("query_stock_positions", {"account_id": account_id},
                       timeout=_MCP_TIMEOUT_QUERY), "query_stock_positions")

    def query_stock_orders(self, *, account_id, stock_code: str = ""):
        return self._rows(
            self._call("query_stock_orders",
                       {"account_id": account_id, "stock_code": stock_code},
                       timeout=_MCP_TIMEOUT_QUERY), "query_stock_orders")

    def order_stock(self, *, account_id, stock_code, order_type, order_volume,
                    price_type, price, notify=0):
        return self._call("order_stock", {
            "account_id": account_id, "stock_code": stock_code, "order_type": order_type,
            "order_volume": int(order_volume), "price_type": price_type,
            "price": float(price), "notify": notify}, timeout=_MCP_TIMEOUT_ORDER)

    def cancel_order_stock(self, *, account_id, stock_code, order_id):
        return self._call("cancel_order_stock", {
            "account_id": account_id, "stock_code": stock_code,
            "order_id": str(order_id)}, timeout=_MCP_TIMEOUT_ORDER)

    def get_stock_info(self, *, stock_code: str):
        """标的静态信息。**不要 account_id**（这是行情侧函数，不属于某个账户），
        字段平铺在 result 上（形状同 `query_stock_asset`）。

        ⚠️ `stock_code` 必须带后缀（`000761.SZ`）：裸 6 位码返回
        `{"ErrorId": "2", "Error": "stock_code error:000761"}`，不是异常、不会自己暴露。
        """
        return self._call("get_stock_info", {"stock_code": stock_code},
                          timeout=_MCP_TIMEOUT_QUERY)

    def get_market_snapshot(self, *, stock_code: str):
        """实时快照（现价/昨收/买卖盘口/成交量）。**不要 account_id**（行情侧函数），
        字段平铺在 result 上（形状同 `get_stock_info`）。

        ⚠️ 与 `get_stock_info` 一样要**带后缀**的代码。ctypes 通道那侧的 tqcenter 实现在
        代码格式非法时会 `tq.close()` 再抛（读它的源码可见）——所以代码必须先归一好再进来。
        """
        return self._call("get_market_snapshot", {"stock_code": stock_code},
                          timeout=_MCP_TIMEOUT_QUERY)

    def close(self) -> None:
        """没有连接可断（HTTP）。会话对象留着复用 keep-alive。"""


class TdxQuantClient:
    """连接管理 + tqcenter 交易查询透传。gateway 层持有它并做领域对象映射。

    实例是**共享连接的把手**，不是连接本身：多个实例指向同一条进程级连接（见模块 docstring）。
    """

    def __init__(self, pyplugins_path: Optional[str] = None, account: str = "",
                 account_type: str = "STOCK", *, transport: Optional[str] = None,
                 mcp_url: Optional[str] = None):
        self._pyplugins = Path(pyplugins_path or _DEFAULT_PYPLUGINS)
        # 类别统一大写：枚举表里是大写（`CREDIT`/`STOCK`），而配置/界面难免写成小写。
        # 归一放在这里而不是调用点，是因为它同时进 `_key`（共享连接身份）——两处写法不同
        # 会让同一个账户被当成两条连接，白白 InitConnect 一轮。
        self._account = (account or "").strip()
        self._account_type = (account_type or "STOCK").strip().upper()
        self._transport = (transport or _DEFAULT_TRANSPORT).strip().lower()
        if self._transport not in TDX_TRANSPORTS:
            raise ValueError(
                f"CN_TDXQUANT_TRANSPORT 只能是 {'/'.join(TDX_TRANSPORTS)}，收到 {self._transport!r}")
        self._mcp_url = (mcp_url or _DEFAULT_MCP_URL).rstrip("/")

    @property
    def _key(self) -> "ConnKey":
        """共享连接的身份。账户/账户类型不同就得重连，不能复用别人的句柄。

        **通道与 MCP 地址也在身份里**：同一个账号在两条通道上是两条不同的连接，
        少了它，切换通道之后会命中上一条通道留下的句柄（`connected` 为真而实际走错路）。
        """
        return ConnKey(self._transport, self._mcp_url, str(self._pyplugins),
                       self._account, self._account_type)

    @property
    def transport(self) -> str:
        """当前通道（`mcp` / `ctypes`）。诊断脚本与探活文案要报它——两条路的故障长得不一样。"""
        return self._transport

    @property
    def mcp_url(self) -> str:
        """mcp 通道的地址。通道自检要拿它做**独立的端口探针**：端口不通＝客户端没开，
        与"开着但交易没登录"是两种完全不同的处置，混成一句话就没人知道该去做哪件事。"""
        return self._mcp_url

    # ── 连接 ─────────────────────────────────────────────
    def prepare_import(self) -> None:
        """装配 DLL 目录与 sys.path，使 `import tqcenter` 可行（不初始化、不取账户句柄）。

        行情源（`sources/tqcenter.py`）复用这一段：它只要 tqcenter 模块本身，不需要交易账户。
        """
        if not self._pyplugins.is_dir():
            raise TdxQuantConnectionError(f"PYPlugins 路径不存在: {self._pyplugins}")
        try:
            os.add_dll_directory(str(self._pyplugins))   # 让 TPythClient.dll 的依赖(tdxrpcx64/mfc/msvc)可加载
        except OSError as e:
            logger.warning("add_dll_directory 失败: %s", e)
        for sub in ("user", "sys"):
            p = str(self._pyplugins / sub)
            if p not in sys.path:
                sys.path.insert(0, p)

    def connect(self) -> None:
        """接上共享连接：命中健康的就复用，否则（重）建。失败前**必定**把全局状态压平。

        `probe` 那一步是这件事的关键：句柄还在不代表连接还活着（交易登录掉了、客户端重启过），
        而不探活就会拿着死句柄去下单，报出来的错和"没连上"长得完全不一样。探活是一次
        `query_stock_asset`，健康时实测 0.01 秒。
        """
        with _LOCK:
            if _SHARED["account_id"] is not None and _SHARED["key"] == self._key:
                if self._probe():
                    return
                logger.warning("TdxQuant 共享连接已失效（探活失败），重连")
            self.hard_reset()                     # 有脏状态先真的断开，别让 initialize 空转
            # 两条通道都要 `prepare_import`：ctypes 为了拿 `tq`，mcp 只为了拿 `tqconst`
            # 那份编号表（纯 Python 常量，不需要 DLL 初始化成功）。见模块 docstring。
            self.prepare_import()
            try:
                if self._transport == "mcp":
                    # ⚠️ **刻意不 import tqcenter**：那个模块顶层要 numpy/pandas 并加载 64 位
                    #    DLL，而 mcp 通道一样都不需要（连接是 HTTP）。编号表用 ast 从厂商
                    #    源码里读，仍然只有厂商一份。见 `tq_constants`。
                    from cn_broker_api.drivers.tdxquant.tq_constants import TqConstants

                    tqconst = TqConstants.load(self._pyplugins)
                    tq = _McpTransport(self._mcp_url)
                else:
                    from tqcenter import tq, tqconst   # 懒加载：此处才需 64 位 + DLL + 客户端
            except Exception as e:  # noqa: BLE001
                raise TdxQuantConnectionError(
                    f"取厂商编号表/导入 tqcenter 失败"
                    f"（确认 64 位 Python + PYPlugins 路径 + 客户端已开）: {e}") from e
            # 先记下 tq：后面任何一步失败，hard_reset 都要靠它清接管标志/断开
            _SHARED["tq"], _SHARED["tqconst"] = tq, tqconst
            try:
                tq.initialize(__file__)            # mcp 通道是空操作
                acc = tq.stock_account(account=self._account, account_type=self._account_type)
            except Exception as e:  # noqa: BLE001
                self.hard_reset()
                raise TdxQuantConnectionError(
                    f"初始化失败（通道 {self._transport}）: {e}") from e
            if acc is None or (isinstance(acc, int) and acc < 0):
                # ★ tqcenter 在这条失败路径上**自己**置了 `_reInitialized`（见其 stock_account 的
                #   ErrorId 6/7 与异常两个分支）。不 reset 把它摘掉的话，之后每次 InitConnect 都去
                #   接管一个不存在的会话、必然失败，而它只在成功时才清零 ⇒ 永久卡死、每次重试白等
                #   15 秒（真机实测）⇒ 只能重启后端。这一行就是那件事的解。
                vendor = getattr(tq, "last_error", "")
                self.hard_reset()
                raise TdxQuantConnectionError(
                    f"获取账户句柄失败（通道 {self._transport} / 资金账号 "
                    f"{self._account or '(默认)'} / 类别 {self._account_type}）"
                    + (f"，厂商原文「{vendor}」" if vendor else "") +
                    f"：① 确认通达信客户端已启动并登录交易（内嵌模拟/实盘）；"
                    f"② **确认类别与账号是一对**——信用（两融）账户必须填 CREDIT，"
                    f"用 STOCK 去查信用账号，厂商报的是「资金账户未登录或不存在」。"
                    f"连接已自动重置，改对后直接再试一次即可，不需要重启后端")
            _SHARED["account_id"], _SHARED["key"] = acc, self._key
            logger.info("TdxQuant 已连接（通道 %s），账户句柄=%s", self._transport, acc)

    def _probe(self) -> bool:
        """共享连接是否还活着（一次 query_stock_asset，看 ErrorId）。异常/空返回都算死。"""
        tq, acc = _SHARED["tq"], _SHARED["account_id"]
        if tq is None or acc is None:
            return False
        try:
            a = tq.query_stock_asset(account_id=acc)
        except Exception as e:  # noqa: BLE001
            logger.warning("TdxQuant 探活异常: %s", str(e)[:120])
            return False
        return bool(a) and str(a.get("ErrorId", "0")) == "0"

    @property
    def connected(self) -> bool:
        """**只看句柄在不在，不探活**——探活要一次 IPC 往返，而这个属性在热路径上被反复读。

        真正的"活着吗"由 `connect()` 在入口探一次；之后同一次操作里出错走 `hard_reset()`。
        """
        return _SHARED["account_id"] is not None and _SHARED["key"] == self._key

    @property
    def account_id(self) -> Optional[int]:
        return _SHARED["account_id"]

    @property
    def account_type(self) -> str:
        """已归一（大写）的账户类别。网关据它判断能不能下信用委托。"""
        return self._account_type

    @property
    def tqconst(self):
        return _SHARED["tqconst"]

    def _require(self):
        if not self.connected:
            raise TdxQuantConnectionError("未连接，请先 connect()")

    # ── 交易/查询透传（返回 tqcenter 原始 dict/list，映射在 gateway 层）────
    def order_stock(self, stock_code: str, order_type: int, volume: int, price_type: int, price: float, notify: int = 0):
        with _LOCK:
            self._require()
            return _SHARED["tq"].order_stock(
                account_id=_SHARED["account_id"], stock_code=stock_code, order_type=order_type,
                order_volume=int(volume), price_type=price_type, price=float(price), notify=notify)

    def cancel(self, stock_code: str, order_id: str):
        with _LOCK:
            self._require()
            return _SHARED["tq"].cancel_order_stock(
                account_id=_SHARED["account_id"], stock_code=stock_code, order_id=str(order_id))

    def query_asset(self) -> Dict[str, Any]:
        with _LOCK:
            self._require()
            return _SHARED["tq"].query_stock_asset(account_id=_SHARED["account_id"]) or {}

    def query_positions(self) -> Optional[List[Dict[str, Any]]]:
        """持仓原始行。🔴 **返回 None 表示「成功但没内容」（查不到），空表 [] 才是真空仓**。

        原先 `or []` 把两者揉成一个：查询抖一下返回 None 会被读成「全部清仓」，
        对账那侧会整表删持仓账本、执行那侧会按从零建仓重复买入。三态判据见
        `gateway.cn.broker_reads` 模块 docstring——区别必须在**源头**保留，
        丢掉之后下游谁都补不回来。
        """
        with _LOCK:
            self._require()
            res = _SHARED["tq"].query_stock_positions(account_id=_SHARED["account_id"])
        return None if res is None else list(res)

    def query_orders(self, stock_code: str = "") -> Optional[List[Dict[str, Any]]]:
        """当日委托原始行。None＝查不到（判不了），[]＝今天真的一笔委托都没有。理由同上。"""
        with _LOCK:
            self._require()
            res = _SHARED["tq"].query_stock_orders(
                account_id=_SHARED["account_id"], stock_code=stock_code)
        return None if res is None else list(res)

    def query_stock_info(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """标的静态信息原始 dict（`ErrorId != 0` ⇒ None＝判不了）。

        与其它查询不同，**这条不需要账户句柄**（行情侧函数），但仍走同一把锁与同一条连接：
        两个通道的 `tq` 都是进程级单条，绕过去没有好处。
        """
        with _LOCK:
            self._require()
            res = _SHARED["tq"].get_stock_info(stock_code=str(stock_code))
        if not isinstance(res, dict) or str(res.get("ErrorId", "0")) != "0":
            return None
        return res

    def query_snapshot(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """实时快照原始 dict（`ErrorId != 0` ⇒ None＝判不了）。

        与 `query_stock_info` 同形（行情侧、不要账户句柄、同一把锁）。**唯一的用途是通道自检**：
        它读的是**客户端自己那条行情**，而项目里所有别的行情都走 `sources/tdx.py`（自研 socket
        直连公网行情服务器，与客户端毫无关系）⇒ 只有这条能回答「客户端的行情侧通不通」，
        而报单恰恰会被客户端的行情状态挡住（「当前客户端行情处于断开状态」）。
        """
        with _LOCK:
            self._require()
            res = _SHARED["tq"].get_market_snapshot(stock_code=str(stock_code))
        if not isinstance(res, dict) or str(res.get("ErrorId", "0")) != "0":
            return None
        return res

    # ── 释放 ─────────────────────────────────────────────
    def close(self) -> None:
        """**软释放：什么都不做，共享连接留着。**

        看着奇怪，但这正是要的行为。调用方是"现构现连、用完即断"的写法，`finally` 里都有
        `disconnect()`；如果这里真的断，一次调仓就会 InitConnect/CloseConnect 三四轮
        （实测每次 close 恒 0.5 秒），还会和调度器抢同一个策略名。连接留着由进程退出时
        tqcenter 自己的 atexit 收尾，出故障时由 `hard_reset()` 收尾。
        """

    def hard_reset(self) -> None:
        """把 tqcenter 的全局状态压平，使**下一次 `initialize()` 是一次干净的新连接**。幂等。

        只做两件被实测证明安全的事（各条路径的实测结果见模块 docstring 的表）：

        1. **`_reInitialized = False`**。它是 `InitConnect` 的"接管已有会话"标志，
           tqcenter 自己会在 `stock_account` 失败时把它置真，而它**只在成功时才清零**
           ⇒ 不手动清就会一直去接管一个不存在的会话、永久卡死，只能重启进程。
           这一行就是"不用再重启后端"。
           **绝不能反过来主动调 `_reInitialize()`**——`close()` 之后调它就是那个毒药，
           实测连 InitConnect 都失败，而且事后把标志改回去也救不回来（损坏在 DLL 侧）。
        2. **只在 tqcenter 认为还连着时才 `close()`**。这个判断不能省：对一条已经断掉的
           会话再 `CloseConnect` 一次，实测会**无限挂住**（比它要修的 15 秒失败更糟）。
           所以这里既不猜也不还原 run_id，`_initialized` 说断了就当断了。

        ⚠️ 会一并断掉行情源 `sources/tqcenter.py` 的连接（同一条底层连接）。那边每个方法都会
        先调 `_auto_initialize()`，故下次取数会自己重连，不需要在这里协调。

        ⭐ **mcp 通道上这两件事都不存在**（HTTP 无状态、没有接管标志、没有会话可重复断），
        所以整段跳过、只清共享句柄。这里显式判类型而不是让 `_McpTransport` 长出
        `_reInitialized` / `_initialized` 假装成 `tq`——那种"假装"会让下一个读代码的人
        以为 MCP 也有那套雷，而这里恰恰是它最大的好处。
        """
        tq = _SHARED["tq"]
        if tq is not None and not isinstance(tq, _McpTransport):
            try:
                tq._reInitialized = False                      # ① 摘掉接管标志
            except Exception as e:  # noqa: BLE001 — 动的是厂商内部字段，尽力而为
                logger.warning("TdxQuant hard_reset 清接管标志失败（已忽略）: %s", str(e)[:120])
            if getattr(tq, "_initialized", False):              # ② 只断一次，别重复断
                try:
                    tq.close()
                except Exception as e:  # noqa: BLE001
                    logger.warning("TdxQuant hard_reset 断开失败（已忽略）: %s", str(e)[:120])
        _SHARED.update(account_id=None, tqconst=None, key=None)

"""自检那一侧的最小通道：MCP over HTTP。**只回答「交易通道通不通」**（三十行），
真交易走 `client.py` 那份完整的。

`TPyth.dll` 自带一个 JSON-RPC 服务，端口属主就是客户端进程 ⇒ **没客户端就没服务端**。
方法名与 `tqcenter` 的函数名一字不差。

三条不能省的口径：

① **传输层失败与业务失败分开**：连不上/非 2xx/协议错抛异常，`ErrorId != 0` 照常返回。
   混成一种，查询类就再也没法把「查不到」和「真的没有」分开（对账整表删持仓那一类错）。
② **`urllib` 必须显式给空 `ProxyHandler`**：本机系统代理在注册表里，`127.0.0.1` 靠
   `<local>` 例外躲过去只是侥幸。这台机器上还有一层接管所有 TCP 的 TUN ⇒ 也不能拿 TCP
   连通性当判据。
③ **拿到句柄不等于登录了交易**：实测有 `ErrorId=0` 而 `Value:[]` 的形态 ⇒ 判据是
   **资产里有资金字段**，不是"调用没报错"。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

#: 查询类超时。客户端偶尔卡一下，但十几秒还不回就是真出事了。
TIMEOUT_QUERY = 15.0

#: 判「登录了交易」看这几个键里有没有一个。**不是看调用成不成功**（见模块 docstring ③）。
MONEY_KEYS = ("Asset", "Balance", "Cash")


class TransportError(Exception):
    """传输层失败：连不上、HTTP 非 2xx、返回体不是 JSON。**与业务失败严格分开。**"""


class McpClient:
    """一条到客户端的 JSON-RPC 通道。轻到可以随用随建（HTTP 无连接可言）。"""

    transport = "mcp"

    def __init__(self, mcp_url: str = "http://127.0.0.1:17709") -> None:
        self.mcp_url = mcp_url.rstrip("/")
        self._seq = 0
        #: 最近一次**业务失败**的厂商原文。「资金账户未登录或不存在」这类话是排查的全部
        #: 线索，吞掉就只剩一句我们自己的猜测。
        self.last_error: str = ""
        #: ⚠️ 空 ProxyHandler ＝ requests 那边 `trust_env=False` 的标准库版本（见 ②）。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, params: Dict[str, Any], *,
             timeout: float = TIMEOUT_QUERY) -> Dict[str, Any]:
        """一次 JSON-RPC 调用，返回 `result` 字典（业务成败留给调用方判）。"""
        self._seq += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": self._seq,
                              "method": method, "params": params}).encode("utf-8")
        req = urllib.request.Request(self.mcp_url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 — 任何一种都是"没说上话"
            raise TransportError(
                f"MCP 调用 {method} 失败（{self.mcp_url}）：{type(e).__name__}: {str(e)[:160]}"
                f"——确认通达信客户端已启动、量化模块已加载（该端口由 TPyth.dll 提供）") from e
        if isinstance(body, dict) and body.get("error"):
            err = body["error"] or {}
            raise TransportError(
                f"MCP 拒绝 {method}：{err.get('message')}（code={err.get('code')}）"
                f"——方法名必须与 tqcenter 函数名一致")
        res = body.get("result") if isinstance(body, dict) else None
        if not isinstance(res, dict):
            raise TransportError(f"MCP {method} 返回体不是对象：{str(body)[:200]}")
        return res

    @staticmethod
    def ok(res: Dict[str, Any]) -> bool:
        return str(res.get("ErrorId", "0")) == "0"

    def _note(self, res: Dict[str, Any], method: str) -> None:
        self.last_error = str(res.get("Error") or res.get("Msg") or res)[:200]

    # ── 方法名与 tqcenter 对齐，勿改 ─────────────────────
    def stock_account(self, account: str = "", account_type: str = "STOCK") -> Optional[int]:
        """要交易账户句柄。**拿不到返回 None**，让上层去出那句"账号与类别是不是一对"的文案。

        ⭐ 类别统一大写：枚举表里是大写（`STOCK`/`CREDIT`），而配置难免写成小写。
        """
        res = self.call("stock_account",
                        {"account": (account or "").strip(),
                         "account_type": (account_type or "STOCK").strip().upper()})
        if not self.ok(res):
            self._note(res, "stock_account")
            return None
        v = res.get("Value")
        return int(v) if isinstance(v, (int, float)) else None

    def query_stock_asset(self, account_id: int) -> Dict[str, Any]:
        """账户资产。字段**平铺在 result 上**（不像持仓/委托包在 Value 里）。整个给出去，
        `ErrorId` 的判断留在上层。"""
        return self.call("query_stock_asset", {"account_id": account_id})

    def get_market_snapshot(self, stock_code: str) -> Dict[str, Any]:
        """实时快照（现价/昨收/买卖盘口）。**不要 account_id**（行情侧函数）。

        ⚠️ 代码必须带后缀（`000001.SZ`）：裸 6 位码返回
        `{"ErrorId": "2", "Error": "stock_code error:000001"}`，不抛异常、不会自己暴露。
        """
        return self.call("get_market_snapshot", {"stock_code": stock_code})

    def query_snapshot(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """快照的三态版：`ErrorId` 非 0 ⇒ None（判不了），否则给出平铺的字段字典。

        ⭐ 名字与母项目那份客户端的同名方法一致，**为的是让 `health.check_quote` 能逐字节
        搬过来**——那段判据 2026-08-21 刚按实盘修过并做过负向验证，retype 一遍等于把验证作废。
        """
        res = self.get_market_snapshot(stock_code)
        if not self.ok(res):
            self._note(res, "get_market_snapshot")
            return None
        return res


def has_money_fields(asset: Optional[Dict[str, Any]]) -> bool:
    """资产里有没有任何一个资金字段。

    🔴 这是「登录了交易」的**唯一**判据。实测 `query_stock_asset` 会返回
    `{"ErrorId": "0", "Value": []}` 这种"成功但没内容"的形态，`if not asset` 拦不住它
    （dict 非空）。照旧往下走会把它读成权益 0 —— **「取不到」与「权益是 0」是两件事**。
    """
    return bool(asset) and any(k in asset for k in MONEY_KEYS)

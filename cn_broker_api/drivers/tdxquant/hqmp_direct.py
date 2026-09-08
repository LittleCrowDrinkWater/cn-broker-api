"""不启动 Tdxw，直接托管 TC 的 HQMP 会话。"""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import struct
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .headless import (
    LAB_MARKER,
    _running_trade_processes,
    _verify_lab_routing,
    _verify_ports_available,
)
from .hqmp_capture import SUPPORTED_DLL_SHA256
from .tq_constants import TqConstants
from cn_broker_api.symbols import market_of, symbol_key
from cn_broker_api.trade.ack_unknown import AckUnknown
from cn_broker_api.trade.credit_kind import CREDIT_KIND_SIDE, CreditOrderKind
from cn_broker_api.trade.order_rejected import OrderRejected


READ_ONLY_METHODS = frozenset({
    "DoLevinGN_927",
    "OperateUser_0",
    "DoLevinGN_803",
    "DoLevinGN_807",
    "DoLevinGN_830",
})
TRADE_METHODS = frozenset({"DoLevinGN_909", "DoLevinGN_808"})


class _RpcString(ctypes.Structure):
    _fields_ = (
        ("data", ctypes.c_void_p),
        ("length", ctypes.c_int),
        ("capacity", ctypes.c_int),
    )


_RpcCallback = ctypes.CFUNCTYPE(
    None,
    ctypes.POINTER(_RpcString),
    ctypes.POINTER(_RpcString),
)


@dataclass
class _PendingCall:
    event: threading.Event = field(default_factory=threading.Event)
    value: Any = None


def require_direct_lab_root(root: Path) -> Path:
    """只允许加载带实验标记、且二进制版本已核验的 64 位 DLL。"""
    root = root.resolve()
    dll_path = root / "tdxRpc64.dll"
    required = (root / LAB_MARKER, dll_path, root / "NewTc" / "TC.exe")
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("实验副本缺少必要文件：" + ", ".join(missing))
    if struct.calcsize("P") != 8:
        raise RuntimeError("HQMP 直接宿主必须使用 64 位 Python")
    digest = hashlib.sha256(dll_path.read_bytes()).hexdigest()
    if digest != SUPPORTED_DLL_SHA256:
        raise RuntimeError("tdxRpc64.dll 版本未经核验，拒绝启动直接 HQMP 宿主")
    return root


def _write_response(target: ctypes.POINTER(_RpcString), payload: bytes) -> None:
    response = target.contents
    if not response.data or response.capacity <= len(payload):
        return
    ctypes.memmove(response.data, payload, len(payload))
    ctypes.memset(response.data + len(payload), 0, 1)
    response.length = len(payload)


def _response_for(method: str) -> bytes:
    if method == "RegisterClient":
        body: dict[str, Any] = {"resultType": "int", "result": 1}
    elif method == "GetInjectHwnd":
        body = {"resultType": "HWND", "result": "0000000000000000"}
    elif method == "RawExternSwitch":
        body = {"resultType": "long", "result": 0}
    else:
        body = {"resultType": "nullptr"}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _selected_account(accounts: Any) -> dict[str, Any]:
    if not isinstance(accounts, list):
        raise RuntimeError("OperateUser_0 没有返回账户列表")
    rows = [row for row in accounts if isinstance(row, dict)]
    selected = [row for row in rows if str(row.get("issel", "")) == "1"]
    if len(selected) == 1:
        return selected[0]
    if not selected and len(rows) == 1:
        return rows[0]
    raise RuntimeError(f"无法唯一确定当前账户：账户数={len(rows)}，选中数={len(selected)}")


class HqmpDirectSession:
    """使用厂商 RPC DLL 接收 TC 注册，并直接调用查询、报单和撤单方法。"""

    def __init__(self, root: Path, port: int, *, enable_trade: bool = False):
        self.root = root
        self.port = port
        self.enable_trade = enable_trade
        self.tc_process: subprocess.Popen[bytes] | None = None
        self._rpc: Any = None
        self._callback: Any = None
        self._dll_directories: tuple[Any, ...] = ()
        self._previous_cwd: Path | None = None
        self._client_token: str | None = None
        self._client_registered = threading.Event()
        self._client_ready = threading.Event()
        self._pending: dict[str, _PendingCall] = {}
        self._lock = threading.Lock()
        self._callback_error: str | None = None

    def start(self, *, launch_tc: bool) -> None:
        """启动本地 RPC 服务；可选启动实验副本 TC，绝不启动 Tdxw。"""
        self.root = require_direct_lab_root(self.root)
        if not 1 <= self.port <= 65535:
            raise RuntimeError(f"HQMP 端口无效：{self.port}")
        blockers = _running_trade_processes()
        if blockers:
            details = ", ".join(f"{name}({process_id})" for name, process_id in blockers)
            raise RuntimeError(f"已有交易客户端进程在运行：{details}")
        _verify_ports_available((self.port,))
        _verify_lab_routing(self.root, self.port)

        self._previous_cwd = Path.cwd()
        os.chdir(self.root)
        self._dll_directories = (
            os.add_dll_directory(str(self.root)),
            os.add_dll_directory(str(self.root / "NewTc")),
        )
        self._rpc = ctypes.CDLL(str(self.root / "tdxRpc64.dll"))
        self._configure_rpc()

        @_RpcCallback
        def callback(
            request_ptr: ctypes.POINTER(_RpcString),
            response_ptr: ctypes.POINTER(_RpcString),
        ) -> None:
            try:
                request = request_ptr.contents
                raw = ctypes.string_at(request.data, request.length)
                response = self._handle_request(raw)
            except Exception as exc:
                # ctypes 回调中的异常不能越过原生边界；这里只保留类型，不保存业务正文。
                self._callback_error = type(exc).__name__
                response = _response_for("")
            _write_response(response_ptr, response)

        self._callback = callback
        if not self._rpc.initRemoteRpcServer(0, 0xA00000, self.port, 15, 10):
            raise RuntimeError("initRemoteRpcServer 失败")
        if not self._rpc.registerRpcServerInterface(b"rpcfunction", callback):
            raise RuntimeError("registerRpcServerInterface 失败")
        if not self._rpc.runRpcServerInterface():
            raise RuntimeError("runRpcServerInterface 失败")

        if launch_tc:
            self.tc_process = subprocess.Popen(
                [str(self.root / "NewTc" / "TC.exe")],
                cwd=self.root / "NewTc",
            )

    def _configure_rpc(self) -> None:
        self._rpc.initRemoteRpcServer.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        )
        self._rpc.initRemoteRpcServer.restype = ctypes.c_bool
        self._rpc.registerRpcServerInterface.argtypes = (ctypes.c_char_p, _RpcCallback)
        self._rpc.registerRpcServerInterface.restype = ctypes.c_bool
        self._rpc.runRpcServerInterface.argtypes = ()
        self._rpc.runRpcServerInterface.restype = ctypes.c_bool
        self._rpc.callRpcClientInterfaceByToken.argtypes = (
            ctypes.c_char_p,
            ctypes.POINTER(_RpcString),
            ctypes.POINTER(_RpcString),
        )
        self._rpc.callRpcClientInterfaceByToken.restype = ctypes.c_bool
        self._rpc.unInitRpcServer.argtypes = ()
        self._rpc.unInitRpcServer.restype = None

    def _handle_request(self, raw: bytes) -> bytes:
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("HQMP 请求不是 JSON 对象")
        method = str(body.get("method", ""))
        params = body.get("params", {})
        if not isinstance(params, dict):
            params = {}
        if method == "RegisterClient":
            token = params.get("token")
            if isinstance(token, str) and token:
                self._client_token = token
                self._client_registered.set()
        elif method == "NotifyMsgClient" and str(params.get("MsgType", "")) == "1":
            # 两份完整抓包都在 104、119 之后收到 1，随后才开始账户及交易查询。
            self._client_ready.set()
        elif method == "ReturnValueComp":
            token = str(params.get("token", ""))
            with self._lock:
                pending = self._pending.get(token)
            if pending is not None:
                pending.value = params.get("value")
                pending.event.set()
        return _response_for(method)

    def wait_for_client(self, timeout: float) -> None:
        if not self._client_ready.wait(timeout):
            detail = f"，回调错误={self._callback_error}" if self._callback_error else ""
            phase = "交易登录就绪通知" if self._client_registered.is_set() else "HQMP 客户端注册"
            raise TimeoutError(f"等待 TC {phase}超时{detail}")

    def call(self, method: str, params: dict[str, Any], timeout: float = 20.0) -> Any:
        """调用已核验方法；交易方法还要求会话显式打开交易闸。"""
        if method not in READ_ONLY_METHODS | TRADE_METHODS:
            raise ValueError(f"HQMP 直接宿主不认识方法：{method}")
        if method in TRADE_METHODS and not self.enable_trade:
            raise ValueError(f"HQMP 直接会话尚未打开交易闸：{method}")
        if self._rpc is None or not self._client_token:
            raise RuntimeError("TC 尚未注册到 HQMP 直接宿主")

        # OperateUser_0 是会话枚举：两份成功抓包都要求业务 token 与 TC 注册 token 相同。
        # 其余方法使用独立 token，才能把异步 ReturnValueComp 精确关联到本次调用。
        operation_token = (
            self._client_token
            if method == "OperateUser_0"
            else "927"
            if method == "DoLevinGN_927"
            else f"cn-broker-direct-{uuid.uuid4().hex}"
        )
        request_params = dict(params)
        request_params["token"] = operation_token
        payload = json.dumps(
            {"method": method, "params": request_params, "returnType": ""},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request_buffer = ctypes.create_string_buffer(payload)
        client_token_bytes = self._client_token.encode("utf-8")
        client_token_buffer = ctypes.create_string_buffer(client_token_bytes)
        request = _RpcString(
            ctypes.cast(request_buffer, ctypes.c_void_p),
            len(payload),
            ctypes.sizeof(request_buffer),
        )
        client_token = _RpcString(
            ctypes.cast(client_token_buffer, ctypes.c_void_p),
            len(client_token_bytes),
            ctypes.sizeof(client_token_buffer),
        )
        pending = _PendingCall()
        with self._lock:
            self._pending[operation_token] = pending
        try:
            queued = self._rpc.callRpcClientInterfaceByToken(
                b"callfunction", ctypes.byref(request), ctypes.byref(client_token)
            )
            if not queued:
                raise RuntimeError(f"TC 拒绝接收 HQMP 调用：{method}")
            if not pending.event.wait(timeout):
                if method in TRADE_METHODS:
                    raise AckUnknown(f"{method} 已发给 TC，但等待回调超时，状态未知")
                raise TimeoutError(f"等待只读调用回调超时：{method}")
            return pending.value
        finally:
            with self._lock:
                self._pending.pop(operation_token, None)

    def probe(self, timeout: float = 20.0) -> dict[str, int]:
        """自动映射当前账户，查询持仓、资产和委托，只返回条数统计。"""
        self.call("DoLevinGN_927", {"mode": "1", "semauto": "1"}, timeout)
        accounts = self.call("OperateUser_0", {}, timeout)
        account = _selected_account(accounts)
        qsid = str(account.get("qsid", ""))
        account_id = str(account.get("zjzh", ""))
        if not qsid or not account_id:
            raise RuntimeError("当前账户缺少 qsid 或 zjzh，无法构造只读查询")
        positions = self.call(
            "DoLevinGN_803",
            {"qsid": qsid, "szID": "", "zjzh": account_id, "zqdm": ""},
            timeout,
        )
        assets = self.call(
            "DoLevinGN_830",
            {"qsid": qsid, "szID": "", "zjzh": account_id},
            timeout,
        )
        orders = self.call("DoLevinGN_807", {}, timeout)
        for name, value in (("持仓", positions), ("资产", assets), ("委托", orders)):
            if not isinstance(value, list):
                raise RuntimeError(f"{name}查询没有返回列表")
        return {
            "account_count": len(accounts),
            "position_count": len(positions),
            "asset_row_count": len(assets),
            "order_count": len(orders),
        }

    def place_order(
        self,
        *,
        symbol: str,
        security_name: str,
        side: str,
        size: int,
        price: float,
        credit_kind: CreditOrderKind | None = None,
        timeout: float = 20.0,
    ) -> dict[str, str]:
        """按已解码的 ``DoLevinGN_909`` 契约提交一笔限价委托。"""
        code = symbol_key(symbol)
        if len(code) != 6 or not code.isdigit():
            raise ValueError(f"证券代码必须是 6 位数字，收到 {symbol!r}")
        market = market_of(code)
        if market not in {"SZ", "SH"}:
            raise ValueError(f"直接 HQMP 报单尚未核验 {market} 市场的 setcode")
        side = str(side).strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError(f"side 只能是 buy / sell，收到 {side!r}")
        if int(size) <= 0 or int(size) != size:
            raise ValueError(f"委托数量必须是正整数，收到 {size!r}")
        if not math.isfinite(float(price)) or float(price) <= 0:
            raise ValueError(f"限价必须为正数，收到 {price!r}")
        if not str(security_name).strip():
            raise ValueError("直接 HQMP 报单需要已核验的证券名称")

        account = self._current_account(timeout)
        constants = TqConstants.load(self.root / "PYPlugins")
        if credit_kind is not None:
            expected_side = CREDIT_KIND_SIDE[credit_kind]
            if side != expected_side:
                raise ValueError(
                    f"信用委托 {credit_kind.name} 的方向必须是 {expected_side}，收到 {side}"
                )
            order_type = getattr(constants, credit_kind.value)
        else:
            order_type = constants.STOCK_BUY if side == "buy" else constants.STOCK_SELL
        user_id = str(account.get("userid", ""))
        if not user_id:
            raise RuntimeError("当前账户缺少 userid，无法构造 DoLevinGN_909")
        values = self.call(
            "DoLevinGN_909",
            {
                "szID": user_id,
                "qsid": "0",
                "realzjzh": "",
                "zqdm": code,
                "zqmc": str(security_name).strip(),
                "setcode": "0" if market == "SZ" else "1",
                "bsflag": str(order_type),
                "flag": "0",
                "price": f"{float(price):.6f}",
                "nwtfs": str(constants.PRICE_MY),
                "wtsl": str(int(size)),
                "bwaitans": "1",
            },
            timeout,
        )
        result = self._single_result("报单", values)
        order_id = str(result.get("wtbh", "")).strip()
        message = str(result.get("retinfo", "")).strip()
        if str(result.get("retflag", "")) != "1" or not order_id:
            raise OrderRejected(f"TC 拒绝报单：{message or '未返回委托编号'}", broker_message=message)
        return {"order_id": order_id, "message": message}

    def cancel_order(self, *, order_id: str, timeout: float = 20.0) -> dict[str, str]:
        """按已解码的 ``DoLevinGN_808`` 契约提交撤单。"""
        order_id = str(order_id).strip()
        if not order_id:
            raise ValueError("撤单必须提供委托编号")
        account = self._current_account(timeout)
        account_id = str(account.get("zjzh", ""))
        if not account_id:
            raise RuntimeError("当前账户缺少 zjzh，无法构造 DoLevinGN_808")
        values = self.call(
            "DoLevinGN_808",
            {"wtbh": order_id, "zjzh": account_id},
            timeout,
        )
        result = self._single_result("撤单", values)
        message = str(result.get("errmsg", "")).strip()
        if str(result.get("success", "")).lower() not in {"1", "true"}:
            raise OrderRejected(f"TC 拒绝撤单：{message or '未返回成功标记'}", broker_message=message)
        return {"order_id": order_id, "message": message}

    def _current_account(self, timeout: float) -> dict[str, Any]:
        return _selected_account(self.call("OperateUser_0", {}, timeout))

    @staticmethod
    def _single_result(action: str, values: Any) -> dict[str, Any]:
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise RuntimeError(f"{action}回调不是单行对象")
        return values[0]

    def stop(self) -> None:
        """只关闭本宿主启动的 TC，并释放 RPC 服务。"""
        if self.tc_process is not None and self.tc_process.poll() is None:
            self.tc_process.terminate()
            try:
                self.tc_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.tc_process.kill()
                self.tc_process.wait(timeout=5)
        self.tc_process = None
        if self._rpc is not None:
            self._rpc.unInitRpcServer()
            self._rpc = None
        for directory in self._dll_directories:
            directory.close()
        self._dll_directories = ()
        if self._previous_cwd is not None:
            os.chdir(self._previous_cwd)
            self._previous_cwd = None

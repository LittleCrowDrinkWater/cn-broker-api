"""不启动 Tdxw，直接托管 TC 的 HQMP 会话。"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cn_broker_api.symbols import market_of, symbol_key
from cn_broker_api.trade.ack_unknown import AckUnknown
from cn_broker_api.trade.credit_kind import CREDIT_KIND_SIDE, CreditOrderKind
from cn_broker_api.trade.order_rejected import OrderRejected

from .headless import (
    LAB_MARKER,
    _running_trade_processes,
    _verify_lab_routing,
    _verify_ports_available,
)
from .hqmp_capture import (
    SUPPORTED_DLL_SHA256,
    decode_body,
    encode_body,
    extract_body,
    load_capture_key,
)
from .tc_proxy import FrameBuffer
from .tq_constants import TqConstants


READ_ONLY_METHODS = frozenset({
    "DoLevinGN_927",
    "OperateUser_0",
    "DoLevinGN_809",
    "DoLevinGN_807",
    "DoLevinGN_822",
    "DoLevinGN_920",
    "DoLevinGN_803",
    "DoLevinGN_830",
})
TRADE_METHODS = frozenset({"DoLevinGN_909", "DoLevinGN_808"})


@dataclass
class _PendingCall:
    event: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: Exception | None = None


def require_direct_lab_root(root: Path) -> Path:
    """只允许使用带实验标记、且协议版本已核验的客户端副本。"""
    root = root.resolve()
    dll_path = root / "tdxRpc64.dll"
    required = (root / LAB_MARKER, dll_path, root / "NewTc" / "TC.exe")
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("实验副本缺少必要文件：" + ", ".join(missing))
    digest = hashlib.sha256(dll_path.read_bytes()).hexdigest()
    if digest != SUPPORTED_DLL_SHA256:
        raise RuntimeError("tdxRpc64.dll 版本未经核验，拒绝启动直接 HQMP 宿主")
    return root


def _process_image_path(process_id: int) -> Path | None:
    """读取 Windows 进程镜像路径，用于确认复用的是实验副本 TC。"""
    if not hasattr(ctypes, "windll"):
        return None
    kernel32 = ctypes.windll.kernel32
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    query_path = kernel32.QueryFullProcessImageNameW
    query_path.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    query_path.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = open_process(0x1000, False, process_id)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not query_path(handle, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value).resolve()
    finally:
        close_handle(handle)


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


def _pack_length(length: int) -> bytes:
    if length < 0x100:
        return b"\xd9" + length.to_bytes(1, "big")
    if length < 0x10000:
        return b"\xda" + length.to_bytes(2, "big")
    return b"\xdb" + length.to_bytes(4, "big")


def _extract_guid(frame: bytes) -> bytes | None:
    payload = frame[20:]
    marker = b"callfunction"
    start = payload.find(marker)
    if start < 0:
        return None
    start += len(marker)
    if payload[start:start + 2] != b"\xd9\x24":
        return None
    guid = payload[start + 2:start + 38]
    return guid if len(guid) == 36 else None


def _replace_guid(frame: bytes, guid: bytes) -> bytes:
    marker = b"callfunction"
    start = frame.find(marker)
    if start < 0:
        raise ValueError("HQMP 调用模板缺少 callfunction")
    start += len(marker)
    if len(frame) < start + 36:
        raise ValueError("HQMP 调用模板缺少 36 字节 GUID")
    return frame[:start] + guid + frame[start + 36:]


def _encode_call(template: bytes, obj: dict[str, Any], key: bytes, guid: bytes) -> bytes:
    frame = _replace_guid(template, guid)
    body = encode_body(obj, key)
    payload = frame[20:72] + _pack_length(len(body)) + body
    header = bytearray(frame[:20])
    header[4:8] = len(payload).to_bytes(4, "little")
    header[8:12] = b"\0" * 4
    return bytes(header) + payload


def _json_response(template: bytes, request_id: int, value: dict[str, Any]) -> bytes:
    body = json.dumps(value, ensure_ascii=False, indent=4).encode("utf-8")
    payload = b"\x92\x00" + _pack_length(len(body)) + body + b"\n"
    header = bytearray(template[:20])
    header[4:8] = len(payload).to_bytes(4, "little")
    header[8:12] = request_id.to_bytes(4, "little")
    return bytes(header) + payload


def _response_for(method: str) -> dict[str, Any]:
    if method == "RegisterClient":
        return {"resultType": "int", "result": "1"}
    if method == "GetInjectHwnd":
        return {"resultType": "HWND", "result": "0000000000000000"}
    if method == "RawExternSwitch":
        return {"resultType": "long", "result": "0"}
    return {"resultType": "nullptr"}


def _load_templates(
    capture_path: Path,
    key: bytes,
    required_methods: frozenset[str],
) -> tuple[dict[str, bytes], bytes]:
    templates: dict[str, bytes] = {}
    response_template: bytes | None = None
    with capture_path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("direction") != "tc_to_client":
                continue
            for summary in row.get("frames", []):
                frame = base64.b64decode(summary["base64"], validate=True)
                if response_template is None and summary.get("json"):
                    response_template = frame
                body = extract_body(frame)
                if body is None:
                    continue
                obj = decode_body(body, key)
                method = obj.get("method")
                if method in required_methods and method not in templates:
                    templates[str(method)] = frame
    missing = required_methods - templates.keys()
    if missing:
        raise ValueError(f"抓包缺少 HQMP 调用模板：{sorted(missing)}")
    if response_template is None:
        raise ValueError("抓包缺少 HQMP 外层回执模板")
    return templates, response_template


class HqmpDirectSession:
    """用纯 Python HQMP 帧接收 TC 注册，并直接执行查询、报单和撤单。"""

    def __init__(
        self,
        root: Path,
        port: int,
        capture_path: Path,
        *,
        enable_trade: bool = False,
    ) -> None:
        self.root = root
        self.port = port
        self.capture_path = capture_path
        self.enable_trade = enable_trade
        self.tc_process: subprocess.Popen[bytes] | None = None
        self._key = b""
        self._templates: dict[str, bytes] = {}
        self._response_template = b""
        self._listener: socket.socket | None = None
        self._connection: socket.socket | None = None
        self._server_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._client_registered = threading.Event()
        self._client_ready = threading.Event()
        self._client_token: str | None = None
        self._guid: bytes | None = None
        self._server_error: Exception | None = None
        self._pending: dict[str, _PendingCall] = {}
        self._state_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._account: dict[str, Any] | None = None

    def start(self, *, launch_tc: bool, reuse_tc: bool = False) -> None:
        """在回环地址启动 HQMP 服务；可选启动实验副本 TC，绝不启动 Tdxw。"""
        self.root = require_direct_lab_root(self.root)
        self.capture_path = self.capture_path.resolve()
        if not self.capture_path.is_file():
            raise RuntimeError(f"HQMP 模板抓包不存在：{self.capture_path}")
        if not 1 <= self.port <= 65535:
            raise RuntimeError(f"HQMP 端口无效：{self.port}")
        if launch_tc and reuse_tc:
            raise ValueError("launch_tc 与 reuse_tc 不能同时启用")
        blockers = _running_trade_processes()
        expected_tc = (self.root / "NewTc" / "TC.exe").resolve()
        reusable = bool(blockers) and all(
            name.lower() == "tc.exe" and _process_image_path(process_id) == expected_tc
            for name, process_id in blockers
        )
        if blockers and not (reuse_tc and reusable):
            details = ", ".join(f"{name}({process_id})" for name, process_id in blockers)
            raise RuntimeError(f"已有交易客户端进程在运行：{details}")
        if reuse_tc and not reusable:
            raise RuntimeError("没有找到可复用的实验副本 TC.exe")
        _verify_ports_available((self.port,))
        _verify_lab_routing(self.root, self.port)

        self._key = load_capture_key(self.root / "tdxRpc64.dll")
        required = READ_ONLY_METHODS | (TRADE_METHODS if self.enable_trade else frozenset())
        self._templates, self._response_template = _load_templates(
            self.capture_path, self._key, required
        )
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(1)
        listener.settimeout(0.2)
        self._listener = listener
        self._server_thread = threading.Thread(target=self._serve, daemon=True)
        self._server_thread.start()

        if launch_tc:
            self.tc_process = subprocess.Popen(
                [str(self.root / "NewTc" / "TC.exe")],
                cwd=self.root / "NewTc",
            )

    def _serve(self) -> None:
        assert self._listener is not None
        try:
            while not self._stop_event.is_set():
                try:
                    connection, _ = self._listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop_event.is_set():
                        return
                    raise
                self._serve_connection(connection)
        except Exception as exc:
            self._server_error = exc
            self._client_registered.set()
            self._client_ready.set()
            self._fail_pending(exc)

    def _serve_connection(self, connection: socket.socket) -> None:
        parser = FrameBuffer()
        connection.settimeout(0.2)
        with self._state_lock:
            self._connection = connection
            self._client_token = None
            self._guid = None
            self._account = None
            self._client_registered.clear()
            self._client_ready.clear()
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = connection.recv(64 * 1024)
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop_event.is_set():
                        return
                    raise
                if not chunk:
                    break
                for frame in parser.feed(chunk):
                    self._handle_frame(frame)
        finally:
            with self._state_lock:
                if self._connection is connection:
                    self._connection = None
                    self._client_token = None
                    self._guid = None
            connection.close()
            self._fail_pending(ConnectionError("TC 已断开 HQMP 连接"))

    def _handle_frame(self, frame: bytes) -> None:
        guid = _extract_guid(frame)
        if guid is not None:
            self._guid = guid
        body = extract_body(frame)
        if body is None:
            return
        obj = decode_body(body, self._key)
        method = str(obj.get("method", ""))
        params = obj.get("params", {})
        if not isinstance(params, dict):
            params = {}
        request_id = int.from_bytes(frame[8:12], "little")
        self._send_frame(
            _json_response(self._response_template, request_id, _response_for(method))
        )
        if method == "RegisterClient":
            token = params.get("token")
            if isinstance(token, str) and token:
                self._client_token = token
                self._client_registered.set()
        elif method == "NotifyMsgClient" and str(params.get("MsgType", "")) in {"1", "113"}:
            self._client_ready.set()
        elif method == "ReturnValueComp":
            token = str(params.get("token", ""))
            with self._state_lock:
                pending = self._pending.get(token)
            if pending is not None:
                pending.value = params.get("value")
                pending.event.set()

    def _send_frame(self, frame: bytes) -> None:
        with self._send_lock:
            connection = self._connection
            if connection is None:
                raise ConnectionError("TC 尚未连接 HQMP 直接宿主")
            connection.sendall(frame)

    def _send_call(self, method: str, params: dict[str, Any]) -> None:
        guid = self._guid
        if guid is None:
            raise RuntimeError("尚未取得 TC 的 callfunction GUID")
        obj = {"method": method, "returnType": "", "params": params}
        self._send_frame(_encode_call(self._templates[method], obj, self._key, guid))

    def _fail_pending(self, error: Exception) -> None:
        with self._state_lock:
            pending_calls = list(self._pending.values())
        for pending in pending_calls:
            pending.error = error
            pending.event.set()

    def wait_for_client(self, timeout: float, *, require_ready: bool = True) -> None:
        """等待 TC 注册；冷启动时还等待登录就绪通知。"""
        deadline = time.monotonic() + timeout
        target = self._client_ready if require_ready else self._client_registered
        while not target.wait(min(0.2, max(0.0, deadline - time.monotonic()))):
            if self.tc_process is not None and self.tc_process.poll() is not None:
                raise RuntimeError(f"实验 TC 在注册前退出，退出码={self.tc_process.returncode}")
            if time.monotonic() >= deadline:
                phase = (
                    "交易登录就绪通知"
                    if require_ready and self._client_registered.is_set()
                    else "HQMP 客户端注册"
                )
                raise TimeoutError(f"等待 TC {phase}超时")
        if self._server_error is not None:
            raise RuntimeError(f"HQMP 服务线程失败：{type(self._server_error).__name__}") from self._server_error

    def _operation_token(self, method: str, params: dict[str, Any]) -> str:
        if method == "OperateUser_0":
            assert self._client_token is not None
            return self._client_token
        if method == "DoLevinGN_927":
            return "927"
        if method == "DoLevinGN_809":
            return f"809{params.get('flag', '')}00"
        if method == "DoLevinGN_807":
            return "getall807kcdcount"
        if method == "DoLevinGN_822":
            return "822-1"
        if method == "DoLevinGN_920":
            return "920YMD"
        if method == "DoLevinGN_803":
            return f"803{params.get('qsid', '')}{params.get('zjzh', '')}"
        if method == "DoLevinGN_830":
            return "830-1"
        return str(uuid.uuid4()).upper()

    def call(self, method: str, params: dict[str, Any], timeout: float = 20.0) -> Any:
        """调用已核验方法；交易方法还要求会话显式打开交易闸。"""
        if method not in READ_ONLY_METHODS | TRADE_METHODS:
            raise ValueError(f"HQMP 直接宿主不认识方法：{method}")
        if method in TRADE_METHODS and not self.enable_trade:
            raise ValueError(f"HQMP 直接会话尚未打开交易闸：{method}")
        if self._client_token is None:
            raise RuntimeError("TC 尚未注册到 HQMP 直接宿主")

        with self._call_lock:
            operation_token = self._operation_token(method, params)
            request_params = dict(params)
            request_params["token"] = operation_token
            pending = _PendingCall()
            with self._state_lock:
                self._pending[operation_token] = pending
            try:
                self._send_call(method, request_params)
                if not pending.event.wait(timeout):
                    if method in TRADE_METHODS:
                        raise AckUnknown(f"{method} 已发给 TC，但等待回调超时，状态未知")
                    raise TimeoutError(f"等待只读调用回调超时：{method}")
                if pending.error is not None:
                    raise pending.error
                return pending.value
            finally:
                with self._state_lock:
                    self._pending.pop(operation_token, None)

    def _initialize_account(self, timeout: float) -> tuple[list[Any], dict[str, Any]]:
        self.call("DoLevinGN_927", {"mode": "1", "semauto": "1"}, timeout)
        accounts = self.call("OperateUser_0", {}, timeout)
        account = _selected_account(accounts)
        self._account = account
        return accounts, account

    def probe(self, timeout: float = 20.0) -> dict[str, int]:
        """复现已成功的初始化序列，只返回账户、持仓、资产和委托条数。"""
        accounts, account = self._initialize_account(timeout)
        for flag in ("5", "6"):
            self.call(
                "DoLevinGN_809",
                {"flag": flag, "setcode": "0", "qsid": "0", "zqdm": "", "zjzh": ""},
                timeout,
            )
        orders = self.call("DoLevinGN_807", {}, timeout)
        order_filter = {"setcode": "-1", "wtbh": "", "zqdm": ""}
        self.call("DoLevinGN_822", order_filter, timeout)
        self.call("DoLevinGN_822", order_filter, timeout)
        self.call("DoLevinGN_920", {}, timeout)

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
        self.call("DoLevinGN_822", order_filter, timeout)
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
        if self._account is None:
            _, self._account = self._initialize_account(timeout)
        return self._account

    @staticmethod
    def _single_result(action: str, values: Any) -> dict[str, Any]:
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise RuntimeError(f"{action}回调不是单行对象")
        return values[0]

    def stop(self) -> None:
        """只关闭本宿主启动的 TC，并释放监听端口。"""
        if self.tc_process is not None and self.tc_process.poll() is None:
            self.tc_process.terminate()
            try:
                self.tc_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.tc_process.kill()
                self.tc_process.wait(timeout=5)
        self.tc_process = None
        self._stop_event.set()
        if self._connection is not None:
            try:
                self._connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self._server_thread is not None:
            self._server_thread.join(timeout=3)
            self._server_thread = None

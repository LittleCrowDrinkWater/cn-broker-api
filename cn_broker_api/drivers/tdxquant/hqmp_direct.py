"""不启动 Tdxw，直接托管 TC 的 HQMP 会话。"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
import math
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from cn_broker_api.drivers.tdxquant.fields import order_time as hhmmss_order_time
from cn_broker_api.symbols import market_of, symbol_key
from cn_broker_api.trade.ack_unknown import AckUnknown
from cn_broker_api.trade.credit_kind import CREDIT_KIND_SIDE, CreditOrderKind
from cn_broker_api.trade.order_rejected import OrderRejected
from cn_broker_api.trade.query_unavailable import QueryUnavailable
from cn_broker_api.trade.wire import (
    CANCEL_DONE,
    CANCEL_FILLED,
    CANCEL_TIMEOUT,
    CANCELED,
    FILLED,
    LIVE,
    PARTIALLY_FILLED,
    cancel_result,
    order_row,
)

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


logger = logging.getLogger(__name__)


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
DIRECT_MONEY_FIELDS = frozenset({"keyong", "nmoney", "yu", "zican", "ztzj"})
_NO_ORDER_MESSAGES = ("没有对应的委托", "无对应委托", "未找到对应委托")


@dataclass
class _PendingCall:
    event: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: Exception | None = None


def _row_value(row: dict[str, Any], *keys: str) -> Any:
    """兼容抓包小写字段与 tqcenter 驱动的驼峰字段。"""
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    lower = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        value = lower.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _true_flag(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _order_time(value: Any) -> str | None:
    """直连 HQMP 的 `wtsj` → 契约要的 `HHMMSS`。认不出返回 `None`（＝这一位没有信息）。

    🔴 **`wtsj` 装的是「当日第几秒」，不是 HHMMSS**。2026-09-11 实盘两笔委托给的是 34823
    与 35141，而它们真实的报单时刻是 09:40:23 与 09:45:41（34823 秒 ＝ 9×3600+40×60+23）。
    此前这里照 tqcenter 那条路的口径把 5 位数补零成 `034823` 就当时刻发出去，调用方按
    03:48:23 去比，于是「柜台这笔是不是本行报出去的」永远判否 ⇒ **按代码+委托量的认领
    全线失效**，而失效的表现是"一笔也认不回来"，不报错。

    两种口径不会混：一天里真实的交易时刻按 HHMMSS 写出来最小是 `91500`（9:15 集合竞价），
    而 91500 > 86399 秒 ⇒ 真按 HHMMSS 发来的值在这里一律落进量程外、返回 `None`，
    调用方那侧的规矩是「缺时刻不排除」，退回原来的行为。**宁可没有这一位，不要错的这一位。**
    """
    raw = str(value or "").strip()
    if not raw.isdigit():
        return None
    secs = int(raw)
    if not 0 <= secs < 86400:
        return None
    return f"{secs // 3600:02d}{secs // 60 % 60:02d}{secs % 60:02d}"


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
        if self._server_thread is not None or self._listener is not None:
            raise RuntimeError("HQMP 服务已经启动")
        self._stop_event.clear()
        self._client_registered.clear()
        self._client_ready.clear()
        self._client_token = None
        self._guid = None
        self._server_error = None
        self._account = None
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
        reusable = len(blockers) == 1 and all(
            name.lower() == "tc.exe" and _process_image_path(process_id) == expected_tc
            for name, process_id in blockers
        )
        if blockers and not (reuse_tc and reusable):
            details = ", ".join(f"{name}({process_id})" for name, process_id in blockers)
            raise RuntimeError(f"已有交易客户端进程在运行：{details}")
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
                try:
                    self._serve_connection(connection)
                except Exception as exc:  # noqa: BLE001 — 单连接失败后仍须接受 TC 重连
                    if self._stop_event.is_set():
                        return
                    logger.warning(
                        "[hqmp] TC 连接异常（%s），已清理该连接并继续等待重连",
                        type(exc).__name__,
                    )
        except Exception as exc:
            self._server_error = exc
            logger.exception("[hqmp] 监听线程停止：%s", type(exc).__name__)
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
                    # 单条 TC 连接被恢复流程主动断开，或对端自行重连，都不应杀死
                    # 外层监听线程；清理当前会话后回到 accept() 等下一条连接。
                    break
                if not chunk:
                    break
                for frame in parser.feed(chunk):
                    try:
                        self._handle_frame(frame)
                    except (ValueError, UnicodeError) as exc:
                        # 帧边界已经由 FrameBuffer 确认；单个非业务帧或未知封装解码失败时，
                        # 跳过该帧继续等 RegisterClient，不能把整条 TC 连接一起丢掉。
                        logger.warning(
                            "[hqmp] 跳过无法解码的单帧（%s）",
                            type(exc).__name__,
                        )
        finally:
            with self._state_lock:
                if self._connection is connection:
                    self._connection = None
                    self._client_token = None
                    self._guid = None
                    self._account = None
                    self._client_registered.clear()
                    self._client_ready.clear()
            connection.close()
            self._fail_pending(ConnectionError("TC 已断开 HQMP 连接"))

    def registration_snapshot(self) -> tuple[bool, bool]:
        """返回当前连接和注册状态，不执行恢复动作。"""
        with self._state_lock:
            connected = self._connection is not None
            registered = self._client_registered.is_set() and bool(self._client_token)
        return connected, registered

    def operation_ready(self, expected_account: str = "") -> tuple[bool, str]:
        """检查当前连接能否接受业务调用，不发送 HQMP 请求。

        账户在首次完整探测或业务调用后缓存；缓存存在时仍核对调用方账户。连接断开会
        清空缓存，因此这里不会沿用上一条 TC 连接的账户信息。
        """
        with self._state_lock:
            if self._server_error is not None:
                return False, f"HQMP 服务线程失败：{type(self._server_error).__name__}"
            if self._connection is None:
                return False, "TC 尚未连接到直接 HQMP 宿主"
            if not self._client_registered.is_set() or not self._client_token:
                return False, "TC 尚未注册到直接 HQMP 宿主"
            selected = str((self._account or {}).get("zjzh", "")).strip()
        expected_account = str(expected_account).strip()
        if expected_account and selected and selected != expected_account:
            return False, "TC 当前选中账户与请求账户不一致"
        return True, "HQMP 连接已注册"

    def verified_target_pids(self, names: Sequence[str]) -> dict[int, str]:
        """只返回属于当前实验副本的唯一 TC 进程。

        直接 HQMP 不能仅按进程名接管客户端：生产计划任务可能在服务
        启动后拉起日常目录的同名进程。发现路径不符、Tdxw.exe 或多个
        TC 时失败关闭，不扫它们的窗口，更不提交密码。
        """
        wanted = {str(name).lower() for name in names}
        if wanted != {"tc.exe"}:
            raise RuntimeError("直接 HQMP 登录只允许目标进程 TC.exe")
        expected_tc = (self.root / "NewTc" / "TC.exe").resolve()
        selected: dict[int, str] = {}
        unexpected: list[tuple[str, int]] = []
        for name, process_id in _running_trade_processes():
            if name.lower() == "tc.exe" and _process_image_path(process_id) == expected_tc:
                selected[process_id] = name
            else:
                unexpected.append((name, process_id))
        if unexpected:
            summary = ", ".join(f"{name}({process_id})" for name, process_id in unexpected)
            raise RuntimeError(f"检测到不属于当前实验副本的交易客户端：{summary}")
        if len(selected) > 1:
            raise RuntimeError("当前实验副本存在多个 TC.exe，拒绝猜测会话")
        return selected

    def reset_unregistered_connection(self) -> bool:
        """断开已连接但未注册的 TC，促使它重新执行 HQMP 握手。

        只有显式登录编排会调用本方法；会话状态和健康检查保持纯观察。
        已完成注册的连接绝不会被中断。
        """
        with self._state_lock:
            connection = self._connection
            if connection is None or self._client_registered.is_set():
                return False
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return True

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
                with self._state_lock:
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

    def channel_ok(self, timeout: float = 5.0) -> tuple[bool, str]:
        """用直接 HQMP 的账户与资产结果判断交易登录是否真正可用。"""
        if not self._client_registered.is_set() or self._client_token is None:
            return False, "TC 尚未注册到直接 HQMP 宿主"
        try:
            _, account = self._initialize_account(timeout)
            for flag in ("5", "6"):
                self.call(
                    "DoLevinGN_809",
                    {"flag": flag, "setcode": "0", "qsid": "0", "zqdm": "", "zjzh": ""},
                    timeout,
                )
            qsid = str(account.get("qsid", ""))
            account_id = str(account.get("zjzh", ""))
            if not qsid or not account_id:
                return False, "HQMP 账户结果缺少查询所需字段"
            assets = self.call(
                "DoLevinGN_830",
                {"qsid": qsid, "szID": "", "zjzh": account_id},
                timeout,
            )
        except Exception as exc:  # noqa: BLE001 — 登录过程中未就绪是被观察状态
            return False, f"HQMP 账户与资产校验未通过：{type(exc).__name__}"
        if not isinstance(assets, list) or not any(
            isinstance(row, dict) and DIRECT_MONEY_FIELDS.intersection(row)
            for row in assets
        ):
            return False, "HQMP 资产查询没有返回资金字段"
        return True, "HQMP 账户和资产查询已通过"

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
        security_name: str = "",
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
                # 委托身份只由 zqdm + setcode 确定。zqmc 是 TC 报文中的
                # 可选展示字段；强制调用方传名称会引入第二个标的身份源。
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

    def query_order(self, *, order_id: str, timeout: float = 20.0) -> dict[str, Any] | None:
        """用 807 与 822 两份柜台视图按委托编号查询，并拒绝歧义结果。"""
        order_id = str(order_id).strip()
        if not order_id:
            raise ValueError("查询委托必须提供委托编号")
        observations: list[dict[str, Any]] = []
        failures: list[str] = []
        filters = {"setcode": "-1", "wtbh": order_id, "zqdm": ""}
        calls = (
            ("DoLevinGN_807", filters),
            ("DoLevinGN_822", filters),
        )
        deadline = time.monotonic() + timeout
        for method, params in calls:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failures.append(f"{method}:总查询超时")
                continue
            try:
                values = self.call(method, params, remaining)
            except Exception as exc:  # noqa: BLE001 — 另一份柜台视图仍可能给出有效证据
                failures.append(f"{method}:{type(exc).__name__}")
                continue
            if not isinstance(values, list):
                failures.append(f"{method}:响应不是列表")
                continue
            matches = [
                row for row in values
                if isinstance(row, dict)
                and str(_row_value(row, "wtbh", "Wtbh") or "").strip() == order_id
            ]
            if len(matches) > 1:
                raise QueryUnavailable(f"{method} 返回多条相同委托编号，拒绝猜测目标委托")
            if matches:
                observations.append(matches[0])

        if observations:
            symbols = {
                str(_row_value(row, "zqdm", "Code", "StockCode") or "").strip()
                for row in observations
                if _row_value(row, "zqdm", "Code", "StockCode") not in (None, "")
            }
            if len(symbols) > 1:
                raise QueryUnavailable("807 与 822 对同一委托编号返回了不同证券代码")
            # 822 在 807 之后调用，若两者状态正好跨过柜台更新边界，后一份证据更新。
            return observations[-1]
        if failures:
            raise QueryUnavailable("直接 HQMP 委托查询判不了：" + "、".join(failures))
        return None

    def query_orders(self, *, timeout: float = 20.0) -> list[dict[str, Any]]:
        """合并 807 与 822 两份当日委托视图，后读到的 822 状态优先。

        两份视图都必须成功返回列表。只读一份会把另一份独有的委托误判为不存在，
        对账调用方可能因此重复报单。
        """
        deadline = time.monotonic() + timeout
        views = (
            ("DoLevinGN_807", {}),
            ("DoLevinGN_822", {"setcode": "-1", "wtbh": "", "zqdm": ""}),
        )
        merged: dict[str, dict[str, Any]] = {}
        for method, params in views:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QueryUnavailable("直接 HQMP 当日委托查询总超时")
            try:
                values = self.call(method, params, remaining)
            except Exception as exc:  # noqa: BLE001 — 缺任一视图都不能声称委托簿完整
                raise QueryUnavailable(
                    f"直接 HQMP 当日委托查询失败：{method}:{type(exc).__name__}"
                ) from exc
            if not isinstance(values, list):
                raise QueryUnavailable(f"{method} 当日委托查询没有返回列表")
            if not all(isinstance(row, dict) for row in values):
                raise QueryUnavailable(f"{method} 当日委托包含非对象行")
            seen: set[str] = set()
            for row in values:
                order_id = str(_row_value(row, "wtbh", "Wtbh") or "").strip()
                if not order_id:
                    raise QueryUnavailable(f"{method} 当日委托缺少委托编号")
                if order_id in seen:
                    raise QueryUnavailable(f"{method} 返回重复委托编号")
                seen.add(order_id)
                previous = merged.get(order_id)
                if previous is not None:
                    old_symbol = str(
                        _row_value(previous, "zqdm", "Code", "StockCode") or ""
                    ).strip()
                    new_symbol = str(
                        _row_value(row, "zqdm", "Code", "StockCode") or ""
                    ).strip()
                    if old_symbol and new_symbol and old_symbol != new_symbol:
                        raise QueryUnavailable(
                            "807 与 822 对同一委托编号返回了不同证券代码"
                        )
                merged[order_id] = row
        return list(merged.values())

    def query_positions(self, *, timeout: float = 20.0) -> list[dict[str, Any]]:
        """查询当前账户持仓原始行；账户参数只从已选中账户动态构造。"""
        account = self._current_account(timeout)
        qsid = str(account.get("qsid", "")).strip()
        account_id = str(account.get("zjzh", "")).strip()
        if not qsid or not account_id:
            raise QueryUnavailable("当前账户缺少 qsid 或 zjzh，无法查询持仓")
        values = self.call(
            "DoLevinGN_803",
            {"qsid": qsid, "szID": "", "zjzh": account_id, "zqdm": ""},
            timeout,
        )
        if not isinstance(values, list):
            raise QueryUnavailable("直接 HQMP 持仓查询没有返回列表")
        if not all(isinstance(row, dict) for row in values):
            raise QueryUnavailable("直接 HQMP 持仓包含非对象行")
        return values

    def query_assets(self, *, timeout: float = 20.0) -> list[dict[str, Any]]:
        """查询当前账户资产原始行；返回不可解析时不伪造全零账户。"""
        account = self._current_account(timeout)
        qsid = str(account.get("qsid", "")).strip()
        account_id = str(account.get("zjzh", "")).strip()
        if not qsid or not account_id:
            raise QueryUnavailable("当前账户缺少 qsid 或 zjzh，无法查询资产")
        values = self.call(
            "DoLevinGN_830",
            {"qsid": qsid, "szID": "", "zjzh": account_id},
            timeout,
        )
        if not isinstance(values, list):
            raise QueryUnavailable("直接 HQMP 资产查询没有返回列表")
        if not all(isinstance(row, dict) for row in values):
            raise QueryUnavailable("直接 HQMP 资产包含非对象行")
        return values

    @staticmethod
    def _direct_order_state(row: dict[str, Any]) -> str:
        size = _number(_row_value(row, "wtsl", "WtVol"))
        filled = _number(_row_value(row, "cjsl", "CjVol"))
        status_text = str(
            _row_value(row, "ztsm", "wtztsm", "wtzt", "StatusText", "status_text") or ""
        )
        if size > 0 and filled >= size:
            return FILLED
        if _true_flag(_row_value(row, "cdflag")) or "已撤" in status_text:
            return CANCELED
        if any(word in status_text for word in ("废单", "拒绝", "无效")):
            return "rejected"
        return PARTIALLY_FILLED if filled > 0 else LIVE

    @classmethod
    def _direct_order_row(cls, row: dict[str, Any]) -> dict[str, Any]:
        code = str(_row_value(row, "zqdm", "Code", "StockCode") or "").strip()
        setcode = str(_row_value(row, "setcode") or "").strip()
        market = "SH" if setcode == "1" else ("SZ" if setcode == "0" else market_of(code))
        symbol = f"{code}.{market}" if code else ""
        side_flag = str(_row_value(row, "bsflag", "BSFlag") or "").strip()
        side = "buy" if side_flag == "0" else ("sell" if side_flag == "1" else None)
        return order_row(
            order_id=str(_row_value(row, "wtbh", "Wtbh") or "").strip(),
            symbol=symbol,
            side=side,
            status=cls._direct_order_state(row),
            size=_row_value(row, "wtsl", "WtVol"),
            price=_row_value(row, "wtjg", "WtPrice", "price"),
            filled_size=_row_value(row, "cjsl", "CjVol") or 0,
            avg_fill_price=_row_value(row, "cjjg", "CjPrice") or 0,
            order_time=cls._direct_order_time(row),
        )

    @staticmethod
    def _direct_order_time(row: dict[str, Any]) -> str | None:
        """报单时刻。**两个字段两把尺子，不能共用一个解析器**：直连 HQMP 的 `wtsj` 是当日
        第几秒，tqcenter 那条路的 `Time` 是 HHMMSS（2026-08-25 真柜台验过）。
        用错一把不会报错，只会安静地给出一个差着几小时的时刻。
        """
        secs = _row_value(row, "wtsj")
        if secs not in (None, ""):
            return _order_time(secs)
        return hhmmss_order_time(row)

    @classmethod
    def _direct_order_cancellable(cls, row: dict[str, Any]) -> bool:
        return (
            cls._direct_order_state(row) in {LIVE, PARTIALLY_FILLED}
            and _true_flag(_row_value(row, "kcdflag"))
        )

    def cancel_order_and_wait(
        self,
        *,
        order_id: str,
        symbol: str = "",
        visibility_timeout: float = 10.0,
        settle_timeout: float = 10.0,
        interval: float = 0.2,
        call_timeout: float = 20.0,
    ) -> dict[str, Any]:
        """等委托唯一可见且可撤后提交撤单，再轮询到终态或明确返回状态未知。

        报单回调成功与委托进入撤单索引之间存在真实可见性窗口。这里绝不补发报单；首次撤单
        若明确返回“没有对应委托”，只有重新查到同一编号仍可撤时才允许再试一次撤单。
        撤单回调超时则只对账、不重发，因为请求可能已经执行。
        """
        order_id = str(order_id).strip()
        if not order_id:
            raise ValueError("撤单必须提供委托编号")
        if min(visibility_timeout, settle_timeout, call_timeout) <= 0 or interval < 0:
            raise ValueError("撤单等待时间必须为正数，轮询间隔不能为负数")
        expected_symbol = symbol_key(symbol) if str(symbol).strip() else ""

        def verified(row: dict[str, Any]) -> dict[str, Any]:
            actual_symbol = symbol_key(
                str(_row_value(row, "zqdm", "Code", "StockCode") or "")
            )
            if expected_symbol and actual_symbol != expected_symbol:
                raise ValueError(
                    f"委托 {order_id} 实际证券代码 {actual_symbol or '(空)'} "
                    f"与请求的 {expected_symbol} 不一致，拒绝撤单"
                )
            return row

        visible_deadline = time.monotonic() + visibility_timeout
        last_row: dict[str, Any] | None = None
        while True:
            try:
                row = self.query_order(
                    order_id=order_id,
                    timeout=min(call_timeout, max(0.001, visible_deadline - time.monotonic())),
                )
            except QueryUnavailable:
                row = None
            if row is not None:
                row = verified(row)
                last_row = row
                state = self._direct_order_state(row)
                canonical = self._direct_order_row(row)
                if state == CANCELED:
                    return cancel_result(outcome=CANCEL_DONE, order=canonical,
                                         reason="柜台查询显示委托已经撤销")
                if state == FILLED:
                    return cancel_result(outcome=CANCEL_FILLED, order=canonical,
                                         reason="委托在撤单提交前已经全部成交")
                if state == "rejected":
                    raise OrderRejected("委托在撤单提交前已被柜台拒绝")
                if self._direct_order_cancellable(row):
                    break
            if time.monotonic() >= visible_deadline:
                return cancel_result(
                    outcome=CANCEL_TIMEOUT,
                    order=None if last_row is None else self._direct_order_row(last_row),
                    reason="委托在等待窗口内尚未唯一可见且可撤——状态未定；绝不重复报单",
                )
            time.sleep(interval)

        cancel_attempts = 0
        cancel_was_uncertain = False
        cancel_authorized = True
        settle_deadline = time.monotonic() + settle_timeout
        while True:
            if cancel_authorized and not cancel_was_uncertain and cancel_attempts < 2:
                cancel_authorized = False
                cancel_attempts += 1
                try:
                    self.cancel_order(order_id=order_id, timeout=call_timeout)
                    cancel_was_uncertain = True
                except AckUnknown:
                    # 已发出但没收到回调；此后只能查询，重发可能把“已经受理”误当成“没发出”。
                    cancel_was_uncertain = True
                except OrderRejected as exc:
                    message = str(exc.broker_message or exc)
                    if not any(hint in message for hint in _NO_ORDER_MESSAGES):
                        raise

            try:
                row = self.query_order(
                    order_id=order_id,
                    timeout=min(call_timeout, max(0.001, settle_deadline - time.monotonic())),
                )
            except QueryUnavailable:
                row = None
            if row is not None:
                row = verified(row)
                last_row = row
                state = self._direct_order_state(row)
                canonical = self._direct_order_row(row)
                if state == CANCELED:
                    return cancel_result(outcome=CANCEL_DONE, order=canonical,
                                         reason="柜台已确认撤销")
                if state == FILLED:
                    return cancel_result(outcome=CANCEL_FILLED, order=canonical,
                                         reason="撤单期间委托已全部成交")
                if state == "rejected":
                    raise OrderRejected("撤单对账时发现原委托已被柜台拒绝")
                # 只有明确拒绝“没有对应委托”且重新查到仍可撤，才进行第二次撤单。
                if (not cancel_was_uncertain and cancel_attempts < 2
                        and self._direct_order_cancellable(row)):
                    cancel_authorized = True
                    continue
            if time.monotonic() >= settle_deadline:
                return cancel_result(
                    outcome=CANCEL_TIMEOUT,
                    order=None if last_row is None else self._direct_order_row(last_row),
                    reason="撤单提交后未在等待窗口内观察到终态——状态未定，须继续对账",
                )
            time.sleep(interval)

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

    def require_account(self, expected_account: str, *, timeout: float = 20.0) -> None:
        """确认 TC 当前选中的资金账号与调用方一致。

        空的 ``expected_account`` 代表调用方明确接受 TC 默认账户。不一致时的错误
        不回显任何一边的账号，避免把账户信息带入 HTTP 日志。
        """
        expected_account = str(expected_account).strip()
        if not expected_account:
            return
        selected = str(self._current_account(timeout).get("zjzh", "")).strip()
        if not selected:
            raise QueryUnavailable("直接 HQMP 当前账户缺少资金账号，无法核对请求账户")
        if selected != expected_account:
            raise ValueError("TC 当前选中账户与请求账户不一致，拒绝执行")

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
        self._fail_pending(ConnectionError("HQMP 宿主正在停止"))
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
        with self._state_lock:
            self._connection = None
            self._client_token = None
            self._guid = None
            self._account = None
            self._client_registered.clear()
            self._client_ready.clear()

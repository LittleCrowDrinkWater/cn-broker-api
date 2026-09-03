"""在独立实验副本中托管通达信交易 DLL，不启动完整的 ``Tdxw.exe``。

这个模块只负责厂商 DLL 与 ``TC.exe`` 之间的本地桥接。对外 HTTP 鉴权、账户串行化和
交易语义仍由 cn-broker-api 负责。为了避免误碰日常交易客户端，入口强制要求实验目录标记，
并在任何 ``Tdxw.exe`` / ``TC.exe`` 已经运行时拒绝启动。
"""
from __future__ import annotations

import base64
import configparser
import csv
import ctypes
import os
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Any


LAB_MARKER = ".trade-lab-marker"
ROUTING_KEY = r"Software\Microsoft\RWNode"
ROUTING_MAX_PORT_NAME = "C_TDX_NEWTC_SERVER_MAXPORT"


DataCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_short,
    ctypes.c_short,
    ctypes.c_void_p,
    ctypes.c_short,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_ubyte,
    ctypes.c_int,
)
SwitchCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_short,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_short,
    ctypes.c_short,
    ctypes.c_int,
)
GenericCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_long,
)


def _encode_registry_value(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _decode_registry_value(value: str) -> str:
    return base64.b64decode(value).decode("utf-8")


def _with_trailing_separator(path: Path) -> str:
    value = str(path.resolve())
    return value if value.endswith("\\") else value + "\\"


def require_lab_root(root: Path) -> Path:
    """校验实验副本，标记缺失时绝不尝试加载 DLL。"""
    root = root.resolve()
    required = (
        root / LAB_MARKER,
        root / "TMTconfig.ini",
        root / "TdxCopilot.dll",
        root / "PYPlugins" / "TPyth.dll",
        root / "PYPlugins" / "tdxRpcx64.dll",
        root / "NewTc" / "TC.exe",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("实验副本缺少必要文件：" + ", ".join(missing))
    if struct.calcsize("P") != 8:
        raise RuntimeError("headless 交易宿主必须使用 64 位 Python")
    return root


def read_ports(root: Path) -> tuple[int, int, int]:
    """从实验副本的 TMTconfig.ini 读取 DLL 实际使用的三个端口。"""
    config = configparser.ConfigParser()
    with (root / "TMTconfig.ini").open(encoding="ascii") as stream:
        config.read_file(stream)
    hq_port = config.getint("HQMP", "Port")
    py_port = config.getint("PYMP", "Port")
    mcp_port = config.getint("MCP", "Port")
    if config.getint("PYMP", "HTTP", fallback=1) != 1:
        raise RuntimeError("TMTconfig.ini 的 [PYMP] HTTP 必须为 1")
    for name, port in (("HQMP", hq_port), ("PYMP", py_port), ("MCP", mcp_port)):
        if not 1 <= port <= 65535:
            raise RuntimeError(f"TMTconfig.ini 的 [{name}] Port 无效：{port}")
    if len({hq_port, py_port, mcp_port}) != 3:
        raise RuntimeError("HQMP、PYMP、MCP 必须使用三个不同的端口")
    return hq_port, py_port, mcp_port


def _running_trade_processes() -> list[tuple[str, int]]:
    completed = subprocess.run(
        ["tasklist.exe", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
    )
    blocked_names = {"tdxw.exe", "tc.exe"}
    matches: list[tuple[str, int]] = []
    for row in csv.reader(completed.stdout.splitlines()):
        if len(row) < 2 or row[0].lower() not in blocked_names:
            continue
        try:
            matches.append((row[0], int(row[1])))
        except ValueError:
            continue
    return matches


def _verify_ports_available(ports: tuple[int, ...]) -> None:
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("0.0.0.0", port))
            except OSError as exc:
                raise RuntimeError(f"TCP 端口 {port} 已被占用") from exc


def _read_encoded_registry_value(key: Any, name: str) -> str | None:
    import winreg

    encoded_name = _encode_registry_value(name)
    try:
        encoded_value, _ = winreg.QueryValueEx(key, encoded_name)
    except FileNotFoundError:
        return None
    try:
        return _decode_registry_value(str(encoded_value))
    except Exception as exc:
        raise RuntimeError(f"注册表路由项 {name!r} 不是有效的 Base64 值") from exc


def _verify_lab_routing(root: Path, hq_port: int) -> None:
    if os.name != "nt":
        raise RuntimeError("headless 交易宿主只能在 Windows 上运行")
    import winreg

    lab_path = _with_trailing_separator(root)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ROUTING_KEY) as key:
        path_port = _read_encoded_registry_value(key, lab_path)
        max_port = _read_encoded_registry_value(key, ROUTING_MAX_PORT_NAME)
    expected = str(hq_port)
    if path_port != expected:
        raise RuntimeError(
            f"实验 TC 路由不匹配：{lab_path} 应指向 {hq_port}，实际为 {path_port!r}"
        )
    if max_port is None or int(max_port) < hq_port:
        raise RuntimeError(
            f"TC RPC 最大端口未包含实验端口 {hq_port}：实际为 {max_port!r}"
        )


def preflight(root: Path) -> tuple[Path, int, int, int]:
    """执行所有无副作用的启动前检查。"""
    root = require_lab_root(root)
    hq_port, py_port, mcp_port = read_ports(root)
    blockers = _running_trade_processes()
    if blockers:
        details = ", ".join(f"{name}({process_id})" for name, process_id in blockers)
        raise RuntimeError(f"已有交易客户端进程在运行：{details}")
    _verify_ports_available((hq_port, py_port, mcp_port))
    _verify_lab_routing(root, hq_port)
    return root, hq_port, py_port, mcp_port


def _wait_for_port(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"等待 127.0.0.1:{port} 监听超时")


class HeadlessTradeHost:
    """持有交易 DLL 生命周期，并把 TPyth JSON-RPC 服务暴露在本机端口。"""

    def __init__(self, root: Path, hq_port: int, py_port: int, mcp_port: int):
        self.root = root
        self.hq_port = hq_port
        self.py_port = py_port
        self.mcp_port = mcp_port
        self.tc_process: subprocess.Popen[bytes] | None = None
        self.copilot: ctypes.CDLL | None = None
        self.tpyth: ctypes.CDLL | None = None
        self.callbacks: tuple[object, ...] = ()
        self.dll_directories: tuple[Any, ...] = ()

    def start(self, *, launch_tc: bool) -> None:
        """加载两个 DLL、注册桥接回调并按需启动实验副本的 TC.exe。"""
        # 厂商 DLL 用相对路径读取同目录配置。宿主是独立进程，改变 cwd 不会影响 broker API。
        os.chdir(self.root)
        self.dll_directories = (
            os.add_dll_directory(str(self.root)),
            os.add_dll_directory(str(self.root / "PYPlugins")),
            os.add_dll_directory(str(self.root / "NewTc")),
        )
        self.copilot = ctypes.CDLL(str(self.root / "TdxCopilot.dll"))
        self._configure_copilot()

        @DataCallback
        def data_stub(*_args: object) -> int:
            return 0

        @SwitchCallback
        def switch_stub(*_args: object) -> int:
            return 0

        @GenericCallback
        def generic_bridge(
            function_id: int,
            input_buffer: int,
            input_size: int,
            output_buffer: int,
            output_size: ctypes.POINTER(ctypes.c_int),
            flags: int,
        ) -> int:
            assert self.copilot is not None
            return int(
                self.copilot.TCO_Data(
                    function_id,
                    0,
                    input_buffer,
                    input_size,
                    output_buffer,
                    output_size,
                    flags,
                )
            )

        # ctypes 回调对象必须与 DLL 同寿命，否则下一次原生回调会跳进已释放的地址。
        self.callbacks = (data_stub, switch_stub, generic_bridge)
        callback_addresses = tuple(
            ctypes.cast(callback, ctypes.c_void_p) for callback in self.callbacks
        )
        self.copilot.TCO_RegisterCallBackFunc(*callback_addresses)
        root_bytes = _with_trailing_separator(self.root).encode("mbcs")
        user_bytes = _with_trailing_separator(self.root / "T0002").encode("mbcs")
        started = self.copilot.TCO_StartInit(root_bytes, user_bytes, b"", 0, 0, b"", 0)
        if started != 1:
            raise RuntimeError(f"TCO_StartInit 返回 {started}")
        _wait_for_port(self.hq_port, 15.0)

        self.tpyth = ctypes.CDLL(str(self.root / "PYPlugins" / "TPyth.dll"))
        self._configure_tpyth()
        self.tpyth.TPY_RegisterCallBackFunc(*callback_addresses)
        started = self.tpyth.TPY_StartInit(root_bytes, user_bytes)
        if started != 1:
            raise RuntimeError(f"TPY_StartInit 返回 {started}")
        started = self.tpyth.TPY_StartClientSrv()
        if started != 1:
            raise RuntimeError(f"TPY_StartClientSrv 返回 {started}")
        _wait_for_port(self.py_port, 15.0)
        _wait_for_port(self.mcp_port, 15.0)

        if launch_tc:
            self.tc_process = subprocess.Popen(
                [str(self.root / "NewTc" / "TC.exe")],
                cwd=self.root / "NewTc",
            )

    def _configure_copilot(self) -> None:
        assert self.copilot is not None
        self.copilot.TCO_RegisterCallBackFunc.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self.copilot.TCO_RegisterCallBackFunc.restype = None
        self.copilot.TCO_StartInit.argtypes = (
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_byte,
            ctypes.c_char_p,
            ctypes.c_int,
        )
        self.copilot.TCO_StartInit.restype = ctypes.c_int
        self.copilot.TCO_Data.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_long,
        )
        self.copilot.TCO_Data.restype = ctypes.c_int
        self.copilot.TCO_Uninit.argtypes = ()
        self.copilot.TCO_Uninit.restype = None

    def _configure_tpyth(self) -> None:
        assert self.tpyth is not None
        self.tpyth.TPY_RegisterCallBackFunc.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self.tpyth.TPY_RegisterCallBackFunc.restype = None
        self.tpyth.TPY_StartInit.argtypes = (ctypes.c_char_p, ctypes.c_char_p)
        self.tpyth.TPY_StartInit.restype = ctypes.c_int
        self.tpyth.TPY_StartClientSrv.argtypes = ()
        self.tpyth.TPY_StartClientSrv.restype = ctypes.c_int
        self.tpyth.TPY_StopClientSrv.argtypes = ()
        self.tpyth.TPY_StopClientSrv.restype = None
        self.tpyth.TPY_Uninit.argtypes = ()
        self.tpyth.TPY_Uninit.restype = None

    def stop(self) -> None:
        """只清理由本宿主启动的 TC，并按相反顺序卸载 DLL 服务。"""
        if self.tc_process is not None and self.tc_process.poll() is None:
            self.tc_process.terminate()
            try:
                self.tc_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.tc_process.kill()
                self.tc_process.wait(timeout=5)
        if self.tpyth is not None:
            self.tpyth.TPY_StopClientSrv()
            self.tpyth.TPY_Uninit()
        if self.copilot is not None:
            self.copilot.TCO_Uninit()

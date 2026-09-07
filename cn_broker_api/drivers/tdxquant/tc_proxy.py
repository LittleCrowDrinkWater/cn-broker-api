"""面向实验副本的 HQMP TCP 记录代理。

TC 的 HQMP 协议是私有协议。当前阶段只做字节级透传和记录，不尝试解析、重组或修改
数据；这样即使多个 JSON 共用一次 TCP read，或者一个 JSON 被拆成多次 read，也不会
改变客户端与 TC 之间的语义。
"""
from __future__ import annotations

import argparse
import base64
import json
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import TextIO


DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BUFFER_SIZE = 64 * 1024
FRAME_HEADER_SIZE = 20
FRAME_MAGIC = 0x27
MAX_FRAME_PAYLOAD = 4 * 1024 * 1024


def _json_text(raw: bytes) -> str | None:
    """只在整块本身就是 JSON 时返回可读文本；不对流做有副作用的猜测。"""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, (dict, list)):
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class FrameBuffer:
    """按 HQMP 的 20 字节头从 TCP 字节流中取出完整帧。"""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, payload: bytes) -> list[bytes]:
        self._buffer.extend(payload)
        frames: list[bytes] = []
        while len(self._buffer) >= FRAME_HEADER_SIZE:
            if self._buffer[0] != FRAME_MAGIC:
                raise ValueError("HQMP 帧头不是 0x27")
            payload_size = int.from_bytes(self._buffer[4:8], "little")
            if payload_size > MAX_FRAME_PAYLOAD:
                raise ValueError(f"HQMP 帧过大：{payload_size} bytes")
            frame_size = FRAME_HEADER_SIZE + payload_size
            if len(self._buffer) < frame_size:
                break
            frames.append(bytes(self._buffer[:frame_size]))
            del self._buffer[:frame_size]
        return frames


def _frame_summary(frame: bytes) -> dict[str, object]:
    """提取不改变报文的诊断信息；正文仍以 base64 原样保留。"""
    payload = frame[FRAME_HEADER_SIZE:]
    summary: dict[str, object] = {
        "size": len(frame),
        "base64": base64.b64encode(frame).decode("ascii"),
        "type": frame[1],
        "request_id": int.from_bytes(frame[8:12], "little"),
    }
    for marker in (b"callfunction", b"rpcfunction"):
        if marker in payload:
            summary["rpc"] = marker.decode("ascii")
            break
    json_start = payload.find(b"{")
    if json_start >= 0:
        decoded = _json_text(payload[json_start:])
        if decoded is not None:
            summary["json"] = json.loads(decoded)
    return summary


class TrafficRecorder:
    """线程安全的 JSONL 记录器；每条记录保存原始字节，便于之后重放核对。"""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def record(self, *, connection_id: int, direction: str, payload: bytes,
               frames: list[bytes] | None = None) -> None:
        if not payload:
            return
        row: dict[str, object] = {
            "time": time.time(),
            "connection": connection_id,
            "direction": direction,
            "size": len(payload),
            "base64": base64.b64encode(payload).decode("ascii"),
        }
        decoded = _json_text(payload)
        if decoded is not None:
            row["json"] = json.loads(decoded)
        if frames:
            row["frames"] = [_frame_summary(frame) for frame in frames]
        with self._lock:
            self._stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._stream.flush()


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[socketserver.BaseRequestHandler],
                 upstream: tuple[str, int], recorder: TrafficRecorder) -> None:
        self.upstream = upstream
        self.recorder = recorder
        self._connection_ids = 0
        self._connection_lock = threading.Lock()
        super().__init__(server_address, handler_class)

    def next_connection_id(self) -> int:
        with self._connection_lock:
            self._connection_ids += 1
            return self._connection_ids

    def handle_error(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        """连接断开是交易客户端的正常生命周期，不打印整段线程 traceback。"""
        return


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _ProxyServer)
        connection_id = server.next_connection_id()
        client = self.request
        client.settimeout(None)
        try:
            upstream = socket.create_connection(server.upstream, timeout=10)
        except OSError:
            client.close()
            return
        with upstream:
            upstream.settimeout(None)
            self._relay(client, upstream, connection_id, "client_to_tc")

    def _relay(self, client: socket.socket, upstream: socket.socket, connection_id: int,
               client_direction: str) -> None:
        def copy(source: socket.socket, target: socket.socket, direction: str) -> None:
            # 两个方向可能同时收到半帧，不能共享尚未消费的字节。
            frame_buffer = FrameBuffer()
            try:
                while True:
                    chunk = source.recv(DEFAULT_BUFFER_SIZE)
                    if not chunk:
                        break
                    server = self.server
                    assert isinstance(server, _ProxyServer)
                    try:
                        frames = frame_buffer.feed(chunk)
                    except ValueError:
                        break
                    server.recorder.record(connection_id=connection_id,
                                           direction=direction, payload=chunk,
                                           frames=frames)
                    target.sendall(chunk)
            except (ConnectionError, OSError):
                # TCP 对端关闭和上游短暂断开是实验客户端的正常重连路径；
                # 代理只需关闭对应方向，不应因为记录器自身异常而再抛线程错误。
                pass
            finally:
                try:
                    target.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        reverse_direction = "tc_to_client"
        first = threading.Thread(
            target=copy, args=(client, upstream, client_direction), daemon=True
        )
        second = threading.Thread(
            target=copy, args=(upstream, client, reverse_direction), daemon=True
        )
        first.start()
        second.start()
        first.join()
        second.join()


def serve(*, bind_host: str, bind_port: int, upstream_host: str, upstream_port: int,
          record_path: Path) -> None:
    """启动代理，直到收到 Ctrl+C；只接受回环监听地址。"""
    if bind_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("HQMP 记录代理只允许监听回环地址")
    record_path.parent.mkdir(parents=True, exist_ok=True)
    with record_path.open("a", encoding="utf-8") as stream:
        recorder = TrafficRecorder(stream)
        with _ProxyServer((bind_host, bind_port), _ProxyHandler,
                          (upstream_host, upstream_port), recorder) as server:
            print(f"TC proxy listening on {bind_host}:{bind_port} -> "
                  f"{upstream_host}:{upstream_port}; log={record_path}", flush=True)
            try:
                server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="实验副本 HQMP 字节级记录代理")
    parser.add_argument("--bind-host", default=DEFAULT_BIND_HOST)
    parser.add_argument("--bind-port", type=int, required=True)
    parser.add_argument("--upstream-host", default=DEFAULT_BIND_HOST)
    parser.add_argument("--upstream-port", type=int, required=True)
    parser.add_argument("--record", type=Path, required=True,
                        help="JSONL 记录路径；该文件会包含原始 TCP 内容")
    args = parser.parse_args()
    serve(bind_host=args.bind_host, bind_port=args.bind_port,
          upstream_host=args.upstream_host, upstream_port=args.upstream_port,
          record_path=args.record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

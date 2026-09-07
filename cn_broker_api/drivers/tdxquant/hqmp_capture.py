"""离线读取 HQMP 抓包；默认仅输出方法和字段名，不输出业务值。"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path

from .tc_proxy import FrameBuffer


SUPPORTED_DLL_SHA256 = "f0855f368084235afa2a5d1d19cd1cce326117b623fe503fb26fc2146fc97a0c"
KEY_RVA = 0xBEC88


def load_capture_key(dll_path: Path) -> bytes:
    """仅接受已静态核对版本；密钥从本地 DLL 读取，不写入仓库。"""
    import pefile

    data = dll_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != SUPPORTED_DLL_SHA256:
        raise ValueError("DLL 版本未验证，拒绝使用固定 RVA")
    with pefile.PE(data=data) as pe:
        return pe.get_data(KEY_RVA, 128).split(b"\0", 1)[0]


def swap_words(data: bytes) -> bytes:
    return b"".join(data[offset:offset + 4][::-1] for offset in range(0, len(data), 4))


def decode_body(body: bytes, key: bytes) -> dict:
    """已观察协议使用 Blowfish ECB、小端 32 位字及 ASCII 数字填充。"""
    from Crypto.Cipher import Blowfish

    if not body or len(body) % 8:
        raise ValueError("业务正文长度必须是非零的 8 字节倍数")
    cipher = Blowfish.new(key, Blowfish.MODE_ECB)
    plain = swap_words(cipher.decrypt(swap_words(body)))
    padding = plain[-1] - ord("0")
    if not 1 <= padding <= 8 or plain[-padding:] != bytes([plain[-1]]) * padding:
        raise ValueError("正文填充校验失败")
    result = json.loads(plain[:-padding].decode("gb18030"))
    if not isinstance(result, dict):
        raise ValueError("业务正文不是 JSON 对象")
    return result


def extract_body(frame: bytes) -> bytes | None:
    """仅识别已验证的两种调用封装，跳过心跳、注册和明文返回帧。"""
    payload = frame[20:]
    if payload.startswith(b"\x93\x00\xd9\x30callfunction"):
        offset = 52  # 48 字节的方法标记与 GUID 字符串结束后。
    elif payload.startswith(b"\x92\xabrpcfunction"):
        offset = 13
    else:
        return None
    if len(payload) <= offset:
        raise ValueError("封装缺少正文长度")
    width = {0xD9: 1, 0xDA: 2, 0xDB: 4}.get(payload[offset])
    if width is None or len(payload) < offset + 1 + width:
        raise ValueError("不支持的正文长度封装")
    start = offset + 1 + width
    length = int.from_bytes(payload[offset + 1:start], "big")
    if len(payload) - start != length:
        raise ValueError("正文长度不匹配")
    return payload[start:]


def summarize_capture(capture_path: Path, key: bytes) -> dict:
    parsers = {}
    methods = Counter()
    callbacks = Counter()
    fields = {}
    pending = Counter()
    matched_callbacks = 0
    unmatched_callbacks = 0
    result_fields = {}
    failures = []
    frame_count = 0
    with capture_path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            row = json.loads(line)
            channel = (row["connection"], row["direction"])
            parser = parsers.setdefault(channel, FrameBuffer())
            for frame in parser.feed(base64.b64decode(row["base64"], validate=True)):
                frame_count += 1
                try:
                    body = extract_body(frame)
                    if body is None:
                        continue
                    obj = decode_body(body, key)
                    method = obj["method"]
                    params = obj.get("params", {})
                    if not isinstance(method, str) or not isinstance(params, dict):
                        raise ValueError("调用结构不匹配")
                    methods[method] += 1
                    fields.setdefault(method, set()).update(params)
                    if frame[20:24] == b"\x93\x00\xd9\x30":
                        pending[(method, str(params.get("token")))] += 1
                    if method == "ReturnValueComp":
                        function = str(params.get("functionname", ""))
                        callbacks[function] += 1
                        correlation = (function, str(params.get("token")))
                        if pending[correlation]:
                            pending[correlation] -= 1
                            matched_callbacks += 1
                        else:
                            unmatched_callbacks += 1
                        values = params.get("value", [])
                        if isinstance(values, list):
                            for value in values:
                                if isinstance(value, dict):
                                    result_fields.setdefault(function, set()).update(value)
                except (ValueError, KeyError, UnicodeError):
                    # 异常正文可能带有账户信息，报告只保留行号。
                    failures.append(line_number)
    return {"frames": frame_count, "decoded": sum(methods.values()),
            "methods": dict(methods), "callback_methods": dict(callbacks),
            "parameter_names": {name: sorted(keys) for name, keys in fields.items()},
            "result_field_names": {name: sorted(keys) for name, keys in result_fields.items()},
            "matched_callbacks": matched_callbacks,
            "unmatched_callbacks": unmatched_callbacks,
            "pending_calls": sum(pending.values()),
            "failed_lines": failures,
            "pending_bytes": sum(len(parser._buffer) for parser in parsers.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dll", type=Path, required=True)
    parser.add_argument("captures", type=Path, nargs="+")
    args = parser.parse_args()
    key = load_capture_key(args.dll)
    failed = False
    for path in args.captures:
        report = summarize_capture(path, key)
        print(json.dumps({"capture": path.name, **report}, ensure_ascii=True))
        failed |= bool(report["failed_lines"] or report["pending_bytes"])
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())

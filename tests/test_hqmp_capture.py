import json

import pytest

from cn_broker_api.drivers.tdxquant.hqmp_capture import decode_body, extract_body, load_capture_key, summarize_capture


def test_decodes_little_endian_blocks_and_ascii_padding():
    blowfish = pytest.importorskip("Crypto.Cipher.Blowfish")
    key = b"synthetic-test-key"
    plain = b'{"method":"Query","params":{}}'
    count = 8 - len(plain) % 8
    padded = plain + str(count).encode() * count
    # 显式构造本地字节序，避免与被测 swap_words 共用实现。
    import struct
    words = struct.unpack("<" + "I" * (len(padded) // 4), padded)
    encrypted = blowfish.new(key, blowfish.MODE_ECB).encrypt(struct.pack(">" + "I" * len(words), *words))
    result = struct.pack("<" + "I" * len(words), *struct.unpack(">" + "I" * len(words), encrypted))
    assert decode_body(result, key) == json.loads(plain)
    with pytest.raises(ValueError):
        decode_body(result[:-1], key)


@pytest.mark.parametrize("length", [80, 336])
def test_extracts_body_with_short_and_long_length(length):
    body = b"x" * length
    marker = b"\xd9" + bytes([length]) if length < 256 else b"\xda" + length.to_bytes(2, "big")
    frame = b"\0" * 20 + b"\x93\x00\xd9\x30callfunction" + b"g" * 36 + marker + body
    assert extract_body(frame) == body
    with pytest.raises(ValueError):
        extract_body(frame[:-1])


def test_rejects_unknown_dll_version(tmp_path):
    pytest.importorskip("pefile")
    path = tmp_path / "unknown.dll"
    path.write_bytes(b"not a verified library")
    with pytest.raises(ValueError, match="RVA"):
        load_capture_key(path)


def test_capture_pairs_callback_without_exposing_values(tmp_path):
    import base64
    from cn_broker_api.drivers.tdxquant.hqmp_capture import swap_words
    blowfish = pytest.importorskip("Crypto.Cipher.Blowfish")
    key = b"synthetic-test-key"
    objects = [
        {"method": "Query", "params": {"token": "private-token"}},
        {"method": "ReturnValueComp", "params": {
            "functionname": "Query", "token": "private-token",
            "value": [{"account": "private-account"}]}},
    ]
    frames = []
    for index, obj in enumerate(objects):
        plain = json.dumps(obj).encode()
        padding = 8 - len(plain) % 8
        plain += str(padding).encode() * padding
        body = swap_words(blowfish.new(key, blowfish.MODE_ECB).encrypt(swap_words(plain)))
        prefix = b"\x93\x00\xd9\x30callfunction" + b"g" * 36 if index == 0 else b"\x92\xabrpcfunction"
        payload = prefix + b"\xda" + len(body).to_bytes(2, "big") + body
        frames.append(b"\x27" + bytes([1 - index]) + b"\0\0" + len(payload).to_bytes(4, "little") + b"\0" * 12 + payload)
    rows = [{"connection": 1, "direction": direction,
             "base64": base64.b64encode(fragment).decode()}
            for direction, fragment in [("out", frames[0][:9]), ("out", frames[0][9:]), ("in", frames[1])]]
    path = tmp_path / "capture.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    report = summarize_capture(path, key)
    assert report["decoded"] == 2
    assert report["matched_callbacks"] == 1
    assert report["unmatched_callbacks"] == report["pending_calls"] == report["pending_bytes"] == 0
    assert report["result_field_names"] == {"Query": ["account"]}
    assert "private-" not in json.dumps(report)

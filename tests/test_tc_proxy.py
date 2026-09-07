import io
import json
import base64
import socket
import threading

import pytest

from cn_broker_api.drivers.tdxquant.tc_proxy import (
    FrameBuffer,
    TrafficRecorder,
    _json_text,
    serve,
    _ProxyHandler,
    _ProxyServer,
)


def test_json_text_only_accepts_a_complete_json_value():
    assert _json_text(b'{"method":"RegisterClient"}') == '{"method":"RegisterClient"}'
    assert _json_text(b'{"method":"RegisterClient"}{"method":"Next"}') is None
    assert _json_text(b"partial") is None


def test_recorder_keeps_raw_payload_and_adds_json_when_possible():
    stream = io.StringIO()
    TrafficRecorder(stream).record(
        connection_id=3, direction="client_to_tc", payload=b'{"method":"Ping"}'
    )
    row = json.loads(stream.getvalue())
    assert row["connection"] == 3
    assert row["direction"] == "client_to_tc"
    assert row["size"] == len(b'{"method":"Ping"}')
    assert row["json"] == {"method": "Ping"}
    assert row["base64"]


def test_frame_buffer_handles_coalesced_and_partial_frames():
    first = b"\x27\x00\x00\x00\x03" + b"\x00" * 15 + b"abc"
    second = b"\x27\x01\x00\x00\x01" + b"\x00" * 15 + b"z"
    parser = FrameBuffer()
    assert parser.feed(first[:7]) == []
    assert parser.feed(first[7:] + second) == [first, second]


def test_frame_buffer_rejects_an_invalid_magic():
    with pytest.raises(ValueError, match="帧头"):
        FrameBuffer().feed(b"x" * 20)


def test_relay_keeps_interleaved_direction_fragments_separate():
    first = b"\x27\x00\x00\x00" + (3).to_bytes(4, "little") + b"\x00" * 12 + b"abc"
    second = b"\x27\x01\x00\x00" + (3).to_bytes(4, "little") + b"\x00" * 12 + b"xyz"
    stream = io.StringIO()
    server = object.__new__(_ProxyServer)
    server.recorder = TrafficRecorder(stream)
    handler = object.__new__(_ProxyHandler)
    handler.server = server
    client, client_peer = socket.socketpair()
    upstream, upstream_peer = socket.socketpair()
    for peer in (client_peer, upstream_peer):
        peer.settimeout(2)
    worker = threading.Thread(target=handler._relay,
                              args=(client, upstream, 1, "client_to_tc"), daemon=True)
    worker.start()
    try:
        # 等待每个片段透传后再发送下一个，保证两个方向的半帧确实交错。
        for source, destination, fragment in (
            (client_peer, upstream_peer, first[:10]),
            (upstream_peer, client_peer, second[:10]),
            (client_peer, upstream_peer, first[10:]),
            (upstream_peer, client_peer, second[10:]),
        ):
            source.sendall(fragment)
            received = b""
            while len(received) < len(fragment):
                chunk = destination.recv(len(fragment) - len(received))
                assert chunk
                received += chunk
            assert received == fragment
    finally:
        client_peer.close()
        upstream_peer.close()
        worker.join(timeout=3)
        client.close()
        upstream.close()
    assert not worker.is_alive()
    captured = [(row["direction"], base64.b64decode(frame["base64"]))
                for row in map(json.loads, stream.getvalue().splitlines())
                for frame in row.get("frames", [])]
    assert captured == [("client_to_tc", first), ("tc_to_client", second)]


def test_frame_summary_extracts_json_after_the_wire_prefix():
    from cn_broker_api.drivers.tdxquant.tc_proxy import _frame_summary

    frame = b"\x27\x00\x00\x00" + (36).to_bytes(4, "little") + b"\x00" * 12
    frame += b"\x92\x00\xd9\x00{\"resultType\":\"int\"}\n"
    summary = _frame_summary(frame)
    assert summary["request_id"] == 0
    assert summary["json"] == {"resultType": "int"}


def test_serve_rejects_non_loopback_binding(tmp_path):
    with pytest.raises(ValueError, match="回环"):
        serve(bind_host="0.0.0.0", bind_port=13575,
              upstream_host="127.0.0.1", upstream_port=13576,
              record_path=tmp_path / "traffic.jsonl")

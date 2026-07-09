from __future__ import annotations

import io
import json
import socket

from dagent.worker import StdioJsonlTransport, UnixSocketJsonlTransport
from dagent.schemas import RuntimeFrame


def test_stdio_jsonl_transport_round_trips_frames() -> None:
    input_stream = io.StringIO(json.dumps({"type": "hello", "payload": {"ok": True}}) + "\n")
    output_stream = io.StringIO()
    transport = StdioJsonlTransport(input_stream=input_stream, output_stream=output_stream)

    frame = transport.read_frame()
    transport.write_frame(RuntimeFrame(type="bye", payload={"process_status": "completed", "exit_code": 0}))

    assert frame.type == "hello"
    assert json.loads(output_stream.getvalue().splitlines()[0])["type"] == "bye"


def test_unix_socket_jsonl_transport_round_trips_frames() -> None:
    left, right = socket.socketpair()
    try:
        transport = UnixSocketJsonlTransport.from_socket(left)
        right.sendall(json.dumps({"type": "hello", "payload": {"ok": True}}).encode() + b"\n")

        frame = transport.read_frame()
        transport.write_frame(RuntimeFrame(type="bye", payload={"process_status": "completed", "exit_code": 0}))

        data = right.recv(4096).decode()
        assert frame.type == "hello"
        assert json.loads(data.splitlines()[0])["type"] == "bye"
    finally:
        right.close()
        transport.close()

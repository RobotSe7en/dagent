from __future__ import annotations

import io
import json
import socket

from dagent.result import RunStartedData, RunStreamEvent
from dagent.schemas import RuntimeFrame
from dagent.worker import StdioJsonlTransport, UnixSocketJsonlTransport


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


def test_unix_socket_connect_retries_until_listener_is_ready(monkeypatch, tmp_path) -> None:
    import dagent.worker as worker_module

    attempts: list[str] = []
    closed: list[FakeSocket] = []

    class FakeSocket:
        def __init__(self, exc: OSError | None = None) -> None:
            self.exc = exc

        def connect(self, path: str) -> None:
            attempts.append(path)
            if self.exc is not None:
                raise self.exc

        def makefile(self, *args, **kwargs):
            return io.StringIO()

        def close(self) -> None:
            closed.append(self)

    sockets = iter([
        FakeSocket(FileNotFoundError("not ready")),
        FakeSocket(ConnectionRefusedError("not accepting yet")),
        FakeSocket(),
    ])
    sleeps: list[float] = []
    socket_path = tmp_path / "worker.sock"

    monkeypatch.setattr(worker_module.socket, "socket", lambda *args, **kwargs: next(sockets))
    monkeypatch.setattr(worker_module.time, "sleep", lambda delay: sleeps.append(delay))

    transport = UnixSocketJsonlTransport.connect(socket_path)
    transport.close()

    assert attempts == [str(socket_path), str(socket_path), str(socket_path)]
    assert sleeps == [
        worker_module.UNIX_SOCKET_CONNECT_RETRY_DELAY_SECONDS,
        worker_module.UNIX_SOCKET_CONNECT_RETRY_DELAY_SECONDS,
    ]
    assert len(closed) == 3


def test_stdio_write_event_uses_fast_path_without_runtime_event_revalidation(monkeypatch) -> None:
    import dagent.schemas.runtime as runtime_schema

    def fail_event_adapter():
        raise AssertionError("event payload should already be typed before transport write")

    event = RunStreamEvent(
        type="run.started",
        data=RunStartedData(kind="tool"),
        sequence=1,
        run_id="run_1",
    )
    output_stream = io.StringIO()
    transport = StdioJsonlTransport(input_stream=io.StringIO(), output_stream=output_stream)

    monkeypatch.setattr(runtime_schema, "_event_adapter", fail_event_adapter)

    transport.write_event(event)

    payload = json.loads(output_stream.getvalue())
    assert payload == {
        "type": "event",
        "payload": {
            "type": "run.started",
            "data": {"kind": "tool"},
            "sequence": 1,
            "run_id": "run_1",
        },
    }

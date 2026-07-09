"""Frame transports for the dagent runtime entrypoint."""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from typing import Protocol, TextIO

from dagent.schemas import RuntimeFrame


class RuntimeFrameTransport(Protocol):
    def read_frame(self) -> RuntimeFrame:
        """Read one JSONL runtime frame."""

    def write_frame(self, frame: RuntimeFrame) -> None:
        """Write one JSONL runtime frame."""

    def close(self) -> None:
        """Close transport resources."""


class StdioJsonlTransport:
    """JSONL transport over caller-provided text streams."""

    def __init__(
        self,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ) -> None:
        self._input = input_stream or sys.stdin
        self._output = output_stream or sys.stdout

    def read_frame(self) -> RuntimeFrame:
        line = self._input.readline()
        if not line:
            raise EOFError("runtime control channel closed before a frame was received.")
        return RuntimeFrame.model_validate_json(line)

    def write_frame(self, frame: RuntimeFrame) -> None:
        self._output.write(frame.model_dump_json() + "\n")
        self._output.flush()

    def close(self) -> None:
        return None


class UnixSocketJsonlTransport:
    """JSONL transport over a connected Unix domain socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._socket = sock
        self._reader = sock.makefile("r", encoding="utf-8")
        self._writer = sock.makefile("w", encoding="utf-8")

    @classmethod
    def connect(cls, path: str | Path) -> "UnixSocketJsonlTransport":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(path))
        return cls(sock)

    @classmethod
    def from_socket(cls, sock: socket.socket) -> "UnixSocketJsonlTransport":
        return cls(sock)

    def read_frame(self) -> RuntimeFrame:
        line = self._reader.readline()
        if not line:
            raise EOFError("runtime control socket closed before a frame was received.")
        return RuntimeFrame.model_validate_json(line)

    def write_frame(self, frame: RuntimeFrame) -> None:
        self._writer.write(frame.model_dump_json() + "\n")
        self._writer.flush()

    def close(self) -> None:
        self._reader.close()
        self._writer.close()
        self._socket.close()

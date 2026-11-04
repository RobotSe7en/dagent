"""Bounded, streaming subprocess collection (stdlib-only for the worker)."""

from __future__ import annotations

import codecs
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any

from dagent.capabilities.tools.registry import ToolOutput
from dagent.capabilities.tools.runtime_paths import RuntimePaths


_TAIL_BYTES = 100_000
# Room for the exit-status prefix and stream separators around the output, so
# output kept inline is never shortened again by the caller's display limits.
_INLINE_BYTE_MARGIN = 1024
_INLINE_LINE_MARGIN = 8


def collect_output(
    process: Any,
    *,
    workspace: Path,
    runtime_directory: str,
    max_bytes: int,
    timeout: int,
    cancel: threading.Event | None,
    stop: Callable[[], None],
    files: bool = True,
    inline_bytes: int = _TAIL_BYTES,
    inline_lines: int = 2000,
) -> tuple[ToolOutput, str | None]:
    """Collect stdout/stderr with bounded memory.

    Output within ``inline_bytes``/``inline_lines`` is returned whole and no
    file is written. Beyond them, with ``files`` the complete output (up to
    ``max_bytes``) is streamed to one overflow file whose path is reported but
    never checked; without ``files`` only an in-memory tail of each stream is
    kept and the result is marked partial.
    """

    relative = PurePosixPath(runtime_directory)
    if ".." in relative.parts and not relative.is_absolute():
        raise ValueError("Invalid result directory.")
    workspace = workspace.resolve()
    runtime_paths = RuntimePaths(workspace, runtime_directory)
    root = (runtime_paths.root / "results").resolve()
    root.relative_to(runtime_paths.boundary)
    lock = threading.Lock()
    # Each tail holds its complete stream until the inline limits are crossed.
    inline_bytes = min(inline_bytes, _TAIL_BYTES)
    tails = [bytearray(), bytearray()]
    handles: list[Any] = [None, None]
    paths: list[Path] = []
    failures: list[dict[str, str]] = []
    received = retained = newlines = 0
    overflowed = closed = False

    def warn(exc: OSError) -> None:
        if not failures:
            failures.append(
                {
                    "field": "content",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )

    def write(index: int, data: bytes) -> None:
        nonlocal retained
        allowed = max(0, max_bytes - retained)
        if handles[index] is None or failures or not allowed:
            return
        # Cut only on a UTF-8 character boundary.
        saved = data[:allowed].decode("utf-8", errors="ignore").encode("utf-8")
        try:
            handles[index].write(saved)
            retained += len(saved)
        except OSError as exc:
            warn(exc)

    def open_files() -> None:
        try:
            root.mkdir(parents=True, exist_ok=True)
            for index in range(2):
                fd, path = tempfile.mkstemp(prefix=".shell-", dir=root)
                handles[index] = os.fdopen(fd, "wb")
                paths.append(Path(path))
        except OSError as exc:
            warn(exc)
            return
        for index, tail in enumerate(tails):
            write(index, bytes(tail))

    def drain(index: int, pipe: Any) -> None:
        nonlocal received, newlines, overflowed
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            try:
                chunk = pipe.read(8192)
            except (OSError, ValueError):
                break
            data = decoder.decode(chunk or b"", final=not chunk).encode("utf-8")
            with lock:
                if closed:
                    break
                received += len(data)
                newlines += data.count(b"\n")
                tails[index].extend(data)
                if overflowed:
                    write(index, data)
                elif (received + _INLINE_BYTE_MARGIN > inline_bytes
                        or newlines + _INLINE_LINE_MARGIN > inline_lines):
                    overflowed = True
                    if files:
                        open_files()
                del tails[index][:-_TAIL_BYTES]
            if not chunk:
                break

    threads = [
        threading.Thread(target=drain, args=(i, pipe), daemon=True)
        for i, pipe in enumerate((process.stdout, process.stderr))
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    reason: str | None = None
    while process.poll() is None:
        if cancel is not None and cancel.is_set():
            reason = "cancelled by caller"
            break
        if time.monotonic() >= deadline:
            reason = f"timed out after {timeout} seconds"
            break
        time.sleep(0.02)
    if reason:
        stop()
    # A descendant can keep a pipe open even after its shell exits.
    for thread in threads:
        thread.join(timeout=0.5)
    unfinished = any(thread.is_alive() for thread in threads)
    if unfinished:
        stop()
    with lock:
        closed = True
        for handle in handles:
            if handle is not None:
                try:
                    handle.close()
                except OSError as exc:
                    warn(exc)
    for pipe in (process.stdout, process.stderr):
        pipe.close()
    prefix = f"exit_code={process.returncode}"
    if reason:
        prefix = reason + "\n" + prefix
    # Within the inline limits each tail still holds its complete stream.
    preview = "\n".join(
        part
        for part in (
            prefix,
            *(bytes(tail).decode("utf-8", errors="replace").strip() for tail in tails),
        )
        if part
    )
    saved = overflowed and len(paths) == 2 and not failures
    if not overflowed:
        kept = received
    elif saved:
        kept = retained
    else:
        kept = sum(len(tail) for tail in tails)
    limited = received > kept
    partial = limited or unfinished or bool(reason)
    reference = None
    if saved:
        target: Path | None = None
        try:
            fd, name = tempfile.mkstemp(prefix="shell-", suffix=".txt", dir=root)
            target = Path(name)
            with os.fdopen(fd, "wb") as output:
                output.write((prefix + "\n").encode("utf-8"))
                for stream_name, path in zip(("stdout", "stderr"), paths):
                    output.write(f"[{stream_name}]\n".encode("utf-8"))
                    with path.open("rb") as source:
                        shutil.copyfileobj(source, output, length=65536)
                    output.write(b"\n")
                output.flush()
                os.fsync(output.fileno())
            reference = {
                "type": "dagent_content_reference",
                "path": runtime_paths.relative(target),
                "media_type": "text/plain; charset=utf-8",
                "byte_length": target.stat().st_size,
                "preview": preview,
            }
        except OSError as exc:
            warn(exc)
            if target is not None:
                try:
                    target.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    warn(cleanup_error)
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            warn(exc)
    return ToolOutput(
        preview,
        retention={
            "source_completeness": "partial" if partial or failures else "complete",
            "source_reason": (
                "storage_failure"
                if failures
                else reason
                or (
                    "capture_limit"
                    if limited
                    else "open_pipe"
                    if unfinished
                    else None
                )
            ),
            "received_bytes": received,
            "retained_bytes": kept,
            "storage_warnings": failures,
            "exit_code": process.returncode,
        },
        content_reference=reference,
    ), reason

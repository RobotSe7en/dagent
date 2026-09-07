"""Bounded, streaming subprocess collection (stdlib-only for the worker)."""

from __future__ import annotations

import codecs
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any

from dagent.capabilities.tools.registry import ToolOutput


def collect_output(
    process: Any,
    *,
    workspace: Path,
    runtime_directory: str,
    max_bytes: int,
    timeout: int,
    cancel: threading.Event | None,
    stop: Callable[[], None],
) -> tuple[ToolOutput, str | None]:
    relative = PurePosixPath(runtime_directory)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid result directory.")
    workspace = workspace.resolve()
    root = workspace.joinpath(*relative.parts, "results").resolve()
    root.relative_to(workspace)
    lock = threading.Lock()
    tails = [bytearray(), bytearray()]
    handles: list[Any] = [None, None]
    paths: list[Path] = []
    failures: list[dict[str, str]] = []
    received = retained = 0
    closed = False

    def warn(exc: OSError) -> None:
        if not failures:
            failures.append(
                {
                    "field": "content",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )

    try:
        root.mkdir(parents=True, exist_ok=True)
        for index in range(2):
            fd, path = tempfile.mkstemp(prefix=".shell-", dir=root)
            handles[index] = os.fdopen(fd, "wb")
            paths.append(Path(path))
    except OSError as exc:
        warn(exc)

    def drain(index: int, pipe: Any) -> None:
        nonlocal received, retained
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
                tails[index].extend(data)
                del tails[index][:-100_000]
                allowed = max(0, max_bytes - retained)
                if handles[index] is not None and not failures and allowed:
                    # Cut only on a UTF-8 character boundary.
                    saved = (
                        data[:allowed].decode("utf-8", errors="ignore").encode("utf-8")
                    )
                    try:
                        handles[index].write(saved)
                        retained += len(saved)
                    except OSError as exc:
                        warn(exc)
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
    preview = "\n".join(
        part
        for part in (
            prefix,
            *(bytes(tail).decode("utf-8", errors="replace").strip() for tail in tails),
        )
        if part
    )
    partial = received > retained or unfinished or bool(reason)
    reference = None
    if not failures:
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
            digest = hashlib.sha256()
            with target.open("rb") as source:
                for chunk in iter(lambda: source.read(65536), b""):
                    digest.update(chunk)
            final = root / f"shell-{digest.hexdigest()}.txt"
            os.replace(target, final)
            target = final
            reference = {
                "type": "dagent_content_reference",
                "path": target.relative_to(workspace).as_posix(),
                "media_type": "text/plain; charset=utf-8",
                "byte_length": target.stat().st_size,
                "sha256": digest.hexdigest(),
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
                    if received > retained
                    else "open_pipe"
                    if unfinished
                    else None
                )
            ),
            "received_bytes": received,
            "retained_bytes": retained,
            "storage_warnings": failures,
            "exit_code": process.returncode,
        },
        content_reference=reference,
    ), reason

"""Built-in file tools: read, write, edit, and search."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
import difflib
import fnmatch
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from pathlib import Path

from dagent.capabilities.tools.shell_tools import register_shell_tools
from dagent.capabilities.tools.registry import ToolOutput, ToolRegistry


GREP_EXCLUDED_DIRS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
}
GREP_MAX_MATCHES = 200
GREP_TIMEOUT_SECONDS = 30
LIST_MAX_ENTRIES = 500
MAX_READ_LINES = 2000
MAX_READ_BYTES = 200_000
MAX_EDIT_DIFF_LINES = 50
TRUNCATED = "[TRUNCATED]"

_RG_UNRESOLVED = object()
_rg_path: object = _RG_UNRESOLVED
_UMASK_LOCK = threading.Lock()
_MUTATION_LOCKS_LOCK = threading.Lock()
_MUTATION_LOCKS: dict[Path, threading.RLock] = {}


@dataclass(frozen=True)
class _ReadWindow:
    shown: list[str]
    total: int
    complete_text: str | None
    truncated_by_bytes: bool


def read_file(path: str | Path, offset: int = 1, limit: int | None = None) -> str:
    if offset is None:
        raise TypeError("offset must be an integer.")
    if offset < 1:
        raise ValueError("offset must be at least 1.")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1.")
    max_lines = min(limit, MAX_READ_LINES) if limit is not None else MAX_READ_LINES
    window = _read_utf8_window(Path(path), offset=offset, max_lines=max_lines)
    shown, total, complete_text = window.shown, window.total, window.complete_text
    if offset > 1 and offset > total:
        raise ValueError(f"offset {offset} is beyond end of file ({total} lines).")
    if complete_text is not None:
        return complete_text
    end_line = offset - 1 + len(shown)
    content = "\n".join(shown)
    if end_line < total or window.truncated_by_bytes:
        reason = (
            "read byte limit reached."
            if window.truncated_by_bytes
            else "use offset/limit to read more."
        )
        content += f"\n{TRUNCATED} showing lines {offset}-{end_line} of {total}; {reason}"
    return content


def write_file(path: str | Path, content: str) -> str:
    resolved = Path(path)
    data = content.encode("utf-8")
    with _mutation_lock(resolved):
        _atomic_write(resolved, data)
    return f"Wrote {len(data)} bytes to {resolved}."


def edit_file(path: str | Path, old_string: str, new_string: str) -> str:
    if not old_string:
        raise ValueError("old_string must not be empty.")
    if old_string == new_string:
        raise ValueError("old_string and new_string are identical.")
    resolved = Path(path)
    with _mutation_lock(resolved):
        text, had_bom = _read_utf8(resolved)
        count = text.count(old_string)
        if count == 0:
            raise ValueError(
                f"old_string was not found in {resolved}. Read the file and copy the exact text."
            )
        if count > 1:
            raise ValueError(
                f"old_string matched {count} locations in {resolved}; "
                "include more surrounding context to make it unique."
            )

        first_line = text.count("\n", 0, text.index(old_string)) + 1
        updated = text.replace(old_string, new_string, 1)
        data = updated.encode("utf-8")
        if had_bom:
            data = codecs.BOM_UTF8 + data
        _atomic_write(resolved, data)

    summary = f"Edited {resolved}: 1 replacement at line {first_line}."
    diff = _unified_diff_excerpt(text, updated, path=resolved)
    return f"{summary}\n{diff}" if diff else summary


def list_files(
    path: str | Path = ".",
    depth: int = 3,
    glob: str | None = None,
) -> ToolOutput:
    """List files (and directories) under a path.

    Without ``glob`` this lists directories (trailing ``/``) and files up to
    ``depth`` levels deep. With ``glob`` it lists only files whose name matches
    the pattern. Returns ``(content, entries)`` so DAG nodes can fan out over
    the structured entry list.
    """
    if depth is None:
        raise TypeError("depth must be an integer.")
    if depth < 1:
        raise ValueError("depth must be at least 1.")
    root = Path(path)
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory.")

    entries: list[str] = []
    truncated = False
    for current, dirnames, filenames in os.walk(root):
        level = len(Path(current).relative_to(root).parts)
        dirnames[:] = sorted(name for name in dirnames if name not in GREP_EXCLUDED_DIRS)
        if glob is None:
            for name in dirnames:
                if len(entries) >= LIST_MAX_ENTRIES:
                    truncated = True
                    break
                entries.append(f"{Path(current) / name}/")
        if truncated:
            dirnames[:] = []
            break
        for name in sorted(filenames):
            if glob is None or fnmatch.fnmatch(name, glob):
                if len(entries) >= LIST_MAX_ENTRIES:
                    truncated = True
                    break
                entries.append(str(Path(current) / name))
        if truncated:
            dirnames[:] = []
            break
        if level + 1 >= depth:
            dirnames[:] = []

    content = "\n".join(entries)
    if truncated:
        content += f"\n{TRUNCATED} showing first {len(entries)} entries; more entries exist."
    return ToolOutput(content=content, value=entries)


def grep(path: str | Path, pattern: str, glob: str | None = None) -> str:
    root = Path(path)
    re.compile(pattern)
    rg = _ripgrep_executable()
    if rg is not None:
        return _grep_with_ripgrep(rg, root, pattern, glob)
    return _grep_pure_python(root, pattern, glob)


def _ripgrep_executable() -> str | None:
    global _rg_path
    if _rg_path is _RG_UNRESOLVED:
        _rg_path = shutil.which("rg")
    return _rg_path  # type: ignore[return-value]


def _grep_with_ripgrep(rg: str, root: Path, pattern: str, glob: str | None) -> str:
    args = [
        rg,
        "--no-heading",
        "--with-filename",
        "--line-number",
        "--color=never",
        "--pcre2",
        "--no-ignore",
        "--hidden",
        "--sort",
        "path",
    ]
    for excluded in sorted(GREP_EXCLUDED_DIRS):
        args.extend(["--glob", f"!{excluded}", "--glob", f"!{excluded}/**"])
    if glob:
        args.extend(["--glob", glob])
    args.extend(["--regexp", pattern, str(root)])
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    timed_out = False
    stopped_after_cap = False

    def kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        process.kill()

    timer = threading.Timer(GREP_TIMEOUT_SECONDS, kill_on_timeout)
    timer.start()
    lines: list[str] = []
    try:
        if process.stdout is None:
            raise ValueError("ripgrep stdout was not captured.")
        for line in process.stdout:
            lines.append(line.rstrip("\r\n"))
            if len(lines) > GREP_MAX_MATCHES:
                stopped_after_cap = True
                process.terminate()
                break
        returncode = process.wait()
    finally:
        timer.cancel()
        if process.stdout is not None:
            process.stdout.close()
    stderr = process.stderr.read() if process.stderr is not None else ""
    if process.stderr is not None:
        process.stderr.close()

    if timed_out:
        raise ValueError(f"ripgrep timed out after {GREP_TIMEOUT_SECONDS}s")
    if stopped_after_cap:
        return _capped_matches(lines)
    if returncode == 1:
        return ""
    if returncode != 0:
        detail = stderr.strip() or f"exit code {returncode}"
        raise ValueError(f"ripgrep failed: {detail}")
    return _capped_matches(lines)


def _grep_pure_python(root: Path, pattern: str, glob: str | None) -> str:
    matcher = re.compile(pattern)
    files = [root] if root.is_file() else sorted(
        p for p in root.rglob("*")
        if p.is_file() and not any(part in GREP_EXCLUDED_DIRS for part in p.parts)
    )
    matches: list[str] = []
    for file_path in files:
        if glob and not fnmatch.fnmatch(file_path.name, glob):
            continue
        try:
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if matcher.search(line):
                matches.append(f"{file_path}:{line_number}:{line}")
                if len(matches) > GREP_MAX_MATCHES:
                    return _capped_matches(matches)
    return _capped_matches(matches)


def _capped_matches(lines: list[str]) -> str:
    if len(lines) > GREP_MAX_MATCHES:
        lines = [
            *lines[:GREP_MAX_MATCHES],
            f"{TRUNCATED} grep stopped after {GREP_MAX_MATCHES} matches.",
        ]
    return "\n".join(lines)


def _read_utf8_window(path: Path, *, offset: int, max_lines: int) -> _ReadWindow:
    shown: list[str] = []
    total = 0
    used_bytes = 0
    exact_bytes = bytearray() if offset == 1 else None
    exact_possible = offset == 1
    stopped_showing = False
    truncated_by_bytes = False

    with path.open("rb") as handle:
        prefix = handle.read(8192)
        if b"\0" in prefix:
            raise ValueError(f"{path} is not a UTF-8 text file.")
        handle.seek(0)

        first_line = True
        while True:
            raw = handle.readline(MAX_READ_BYTES + 1)
            if not raw:
                break
            if first_line:
                first_line = False
                if raw.startswith(codecs.BOM_UTF8):
                    raw = raw[len(codecs.BOM_UTF8):]
            line_was_partial = len(raw) > MAX_READ_BYTES and not raw.endswith((b"\n", b"\r"))
            if line_was_partial:
                _discard_line_remainder(handle)
                truncated_by_bytes = True
                exact_possible = False
            total += 1

            if total < offset:
                exact_possible = False
                continue
            if stopped_showing or len(shown) >= max_lines:
                exact_possible = False
                stopped_showing = True
                continue

            line = _line_text(raw)
            line_bytes = len(line.encode("utf-8")) + 1
            if used_bytes + line_bytes > MAX_READ_BYTES:
                if not shown:
                    prefix_lines = _decode_utf8_prefix(raw, MAX_READ_BYTES).splitlines()
                    line = prefix_lines[0] if prefix_lines else ""
                    shown.append(line)
                    used_bytes = len(line.encode("utf-8"))
                truncated_by_bytes = True
                exact_possible = False
                stopped_showing = True
                continue
            used_bytes += line_bytes
            shown.append(line)
            if exact_bytes is not None and exact_possible:
                exact_bytes.extend(raw)

    if exact_possible and exact_bytes is not None and len(shown) == total:
        return _ReadWindow(shown, total, exact_bytes.decode("utf-8", errors="replace"), False)
    return _ReadWindow(shown, total, None, truncated_by_bytes)


def _discard_line_remainder(handle) -> None:
    while True:
        chunk = handle.readline(MAX_READ_BYTES + 1)
        if not chunk or chunk.endswith((b"\n", b"\r")):
            return


def _line_text(raw: bytes) -> str:
    lines = raw.decode("utf-8", errors="replace").splitlines()
    return lines[0] if lines else ""


def _decode_utf8_prefix(raw: bytes, max_bytes: int) -> str:
    prefix = raw[:max_bytes]
    while prefix:
        try:
            return prefix.decode("utf-8")
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return ""


def _read_utf8(path: Path) -> tuple[str, bool]:
    raw = path.read_bytes()
    if b"\0" in raw[:8192]:
        raise ValueError(f"{path} is not a UTF-8 text file.")
    had_bom = raw.startswith(codecs.BOM_UTF8)
    if had_bom:
        raw = raw[len(codecs.BOM_UTF8):]
    return raw.decode("utf-8", errors="replace"), had_bom


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_stat = _existing_regular_file_stat(path)
    mode = _replacement_mode(existing_stat)
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _existing_regular_file_stat(path: Path) -> os.stat_result | None:
    try:
        existing = path.stat()
    except FileNotFoundError:
        return None
    return existing if stat.S_ISREG(existing.st_mode) else None


def _replacement_mode(existing: os.stat_result | None) -> int:
    if existing is not None:
        return stat.S_IMODE(existing.st_mode) & 0o777
    return _default_file_mode()


def _default_file_mode() -> int:
    with _UMASK_LOCK:
        current_umask = os.umask(0)
        os.umask(current_umask)
    return 0o666 & ~current_umask


def _mutation_lock(path: Path) -> threading.RLock:
    key = path.resolve()
    with _MUTATION_LOCKS_LOCK:
        lock = _MUTATION_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _MUTATION_LOCKS[key] = lock
        return lock


def _unified_diff_excerpt(before: str, after: str, *, path: Path) -> str:
    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=str(path),
            tofile=str(path),
            lineterm="",
        )
    )
    if len(diff_lines) > MAX_EDIT_DIFF_LINES:
        omitted = len(diff_lines) - MAX_EDIT_DIFF_LINES
        diff_lines = [*diff_lines[:MAX_EDIT_DIFF_LINES], f"{TRUNCATED} diff omitted {omitted} more lines."]
    return "\n".join(diff_lines)


def register_file_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="read_file",
        handler=read_file,
        action="read",
        path_args=("path",),
        description=(
            "Read a UTF-8 text file. Returns at most "
            f"{MAX_READ_LINES} lines; use offset/limit to page through larger files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read."},
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed).",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read.",
                },
            },
            "required": ["path"],
        },
    )
    registry.register(
        name="write_file",
        handler=write_file,
        action="write",
        path_args=("path",),
        risk="medium",
        description="Write UTF-8 text to a file, replacing any existing content.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write."},
                "content": {"type": "string", "description": "Text content to write."},
            },
            "required": ["path", "content"],
        },
    )
    registry.register(
        name="edit_file",
        handler=edit_file,
        action="write",
        path_args=("path",),
        risk="medium",
        description=(
            "Replace one exact text occurrence in a UTF-8 file. "
            "old_string must match the file content exactly once; "
            "read the file first and include enough surrounding context to make it unique."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to edit."},
                "old_string": {
                    "type": "string",
                    "description": "Exact existing text to replace; must be unique in the file.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text.",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    )
    registry.register(
        name="list_files",
        handler=list_files,
        action="read",
        path_args=("path",),
        default_args={"path": "."},
        description=(
            "List files and directories under a path (directories end with /). "
            "Pass glob (e.g. *.py) to find matching files only. "
            f"Returns at most {LIST_MAX_ENTRIES} entries."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to list.",
                    "default": ".",
                },
                "depth": {
                    "type": "integer",
                    "description": "How many directory levels to include (1 = top level only).",
                    "default": 3,
                },
                "glob": {
                    "type": "string",
                    "description": "Optional filename filter, e.g. *.py; lists matching files only.",
                },
            },
        },
    )
    registry.register(
        name="grep",
        handler=grep,
        action="read",
        path_args=("path",),
        description=(
            "Search text files for a regular expression. "
            "Uses ripgrep when available, with a pure-Python fallback."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory path."},
                "pattern": {"type": "string", "description": "Regular expression."},
                "glob": {
                    "type": "string",
                    "description": "Optional filename filter, e.g. *.py.",
                },
            },
            "required": ["path", "pattern"],
        },
    )


def create_file_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_file_tools(registry)
    register_shell_tools(registry)
    return registry

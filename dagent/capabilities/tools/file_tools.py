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
    source_text: str = ""
    start_char: int = 0


def read_file(path: str | Path, offset: int = 1, limit: int | None = None,
              offset_chars: int | None = None, limit_chars: int | None = None) -> ToolOutput:
    if offset_chars is not None:
        if offset != 1 or limit is not None:
            raise ValueError("Character and line windows cannot be combined.")
        if offset_chars < 0 or (limit_chars is not None and limit_chars < 1):
            raise ValueError("Character offset must be nonnegative and limit positive.")
        size = min(limit_chars or 1024, MAX_READ_BYTES)
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            remaining = offset_chars
            while remaining:
                skipped = handle.read(min(8192, remaining))
                if not skipped:
                    raise ValueError("Character offset is beyond end of file.")
                remaining -= len(skipped)
            text = handle.read(size)
            if "\0" in text:
                raise ValueError(f"{path} is not a UTF-8 text file.")
            bounded = _decode_utf8_prefix(text.encode("utf-8"), MAX_READ_BYTES)
            more = bool(handle.read(1)) or len(bounded) < len(text)
        return _file_output(path, bounded, start=offset_chars, more=more)
    if limit_chars is not None:
        raise ValueError("limit_chars requires offset_chars.")
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
        return _file_output(path, complete_text, start=0, more=False)
    end_line = offset - 1 + len(shown)
    # The window is an exact source prefix, including its line terminators.
    # Stripping them can report EOF while silently losing the final newline.
    content = window.source_text
    body_length = len(content)
    if end_line < total or window.truncated_by_bytes:
        reason = (
            "read byte limit reached."
            if window.truncated_by_bytes
            else "file window limit reached."
        )
        content += (
            f"\n[SOURCE_TRUNCATED] showing lines {offset}-{end_line} of {total}; {reason} "
            f"Continue with offset_chars={window.start_char + body_length}, limit_chars=1024."
        )
    return _file_output(path, content, start=window.start_char,
                        more=end_line < total or window.truncated_by_bytes, body_length=body_length)


def _file_output(path: str | Path, text: str, *, start: int, more: bool,
                 body_length: int | None = None) -> ToolOutput:
    length = len(text) if body_length is None else body_length
    return ToolOutput(content=text, retention={
        "source_completeness": "partial" if more else "complete",
        "source_reason": "file_window" if more else None,
        "window_start": start,
        "window_length": length,
        "continuation": {"tool": "read_file", "path": str(path),
                         "offset": start + length, "limit": 1024},
    })


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
    path: str | Path = ".", depth: int = 3, glob: str | None = None,
    offset: int = 0, limit: int | None = None,
) -> ToolOutput:
    """List one stable window; structured value remains a list of entries."""
    if depth is None:
        raise TypeError("depth must be an integer.")
    if depth < 1:
        raise ValueError("depth must be at least 1.")
    if offset < 0 or (limit is not None and limit < 1):
        raise ValueError("offset must be nonnegative and limit positive.")
    size = min(limit or LIST_MAX_ENTRIES, LIST_MAX_ENTRIES)
    root = Path(path)
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory.")
    entries: list[str] = []
    seen = 0
    for current, dirnames, filenames in os.walk(root):
        level = len(Path(current).relative_to(root).parts)
        dirnames[:] = sorted(name for name in dirnames if name not in GREP_EXCLUDED_DIRS)
        names = ([f"{name}/" for name in dirnames] if glob is None else []) + [
            name for name in sorted(filenames) if glob is None or fnmatch.fnmatch(name, glob)
        ]
        for name in names:
            if seen >= offset:
                entries.append(str(Path(current) / name.rstrip("/")) + ("/" if name.endswith("/") else ""))
            seen += 1
            if len(entries) > size:
                break
        if len(entries) > size:
            break
        if level + 1 >= depth:
            dirnames[:] = []
    more = len(entries) > size
    entries = entries[:size]
    content = "\n".join(entries)
    if more:
        content += f"\n{TRUNCATED} showing {len(entries)} entries; continue with offset={offset + len(entries)}, limit={size}. Files may change between queries."
    return ToolOutput(content=content, value=entries, retention={
        "source_completeness": "partial" if more else "complete",
        "source_reason": "query_window" if more else None,
        "continuation": ({"tool": "list_files", "path": str(path), "offset": offset + len(entries),
                          "limit": size, "glob": glob, "depth": depth} if more else None),
    })


def grep(path: str | Path, pattern: str, glob: str | None = None,
         offset: int = 0, limit: int | None = None) -> ToolOutput:
    if offset < 0 or (limit is not None and limit < 1):
        raise ValueError("offset must be nonnegative and limit positive.")
    size = min(limit or GREP_MAX_MATCHES, GREP_MAX_MATCHES)
    root = Path(path)
    re.compile(pattern)
    rg = _ripgrep_executable()
    if rg is not None:
        content = _grep_with_ripgrep(rg, root, pattern, glob, offset=offset, limit=size)
    else:
        content = _grep_pure_python(root, pattern, glob, offset=offset, limit=size)
    more = content.endswith(f"{TRUNCATED} grep stopped after {size} matches.")
    return ToolOutput(content=content, retention={
        "source_completeness": "partial" if more else "complete",
        "source_reason": "query_window" if more else None,
        "continuation": ({"tool": "grep", "path": str(path), "pattern": pattern,
                          "glob": glob, "offset": offset + size, "limit": size} if more else None),
    })


def _ripgrep_executable() -> str | None:
    global _rg_path
    if _rg_path is _RG_UNRESOLVED:
        _rg_path = shutil.which("rg")
    return _rg_path  # type: ignore[return-value]


def _grep_with_ripgrep(rg: str, root: Path, pattern: str, glob: str | None,
                      *, offset: int = 0, limit: int = GREP_MAX_MATCHES) -> str:
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
        for index, line in enumerate(process.stdout):
            if index < offset:
                continue
            lines.append(line.rstrip("\r\n"))
            if len(lines) > limit:
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
        return _capped_matches(lines, limit)
    if returncode == 1:
        return ""
    if returncode != 0:
        detail = stderr.strip() or f"exit code {returncode}"
        raise ValueError(f"ripgrep failed: {detail}")
    return _capped_matches(lines, limit)


def _grep_pure_python(root: Path, pattern: str, glob: str | None,
                      *, offset: int = 0, limit: int = GREP_MAX_MATCHES) -> str:
    matcher = re.compile(pattern)
    files = (root,) if root.is_file() else _search_files(root)
    matches: list[str] = []
    seen = 0
    for file_path in files:
        if glob and not fnmatch.fnmatch(file_path.name, glob):
            continue
        try:
            handle = file_path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.rstrip("\r\n")
                if matcher.search(line):
                    seen += 1
                    if seen <= offset:
                        continue
                    matches.append(f"{file_path}:{line_number}:{line}")
                    if len(matches) > limit:
                        return _capped_matches(matches, limit)
    return _capped_matches(matches, limit)


def _search_files(root: Path):
    """Path-order traversal without materializing the complete recursive tree."""
    for path in sorted(root.iterdir(), key=lambda path: path.name + ("/" if path.is_dir() else "")):
        if path.is_symlink():
            continue
        if path.is_dir():
            if path.name not in GREP_EXCLUDED_DIRS:
                yield from _search_files(path)
        elif path.is_file():
            yield path


def _capped_matches(lines: list[str], limit: int = GREP_MAX_MATCHES) -> str:
    if len(lines) > limit:
        lines = [
            *lines[:limit],
            f"{TRUNCATED} grep stopped after {limit} matches.",
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
    source_parts: list[str] = []
    character_count = start_char = 0
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

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
            # readline is bounded in bytes, before BOM removal. A chunk ending
            # at CR may still be only the first half of a CRLF terminator.
            line_was_partial = len(raw) > MAX_READ_BYTES and not raw.endswith(b"\n")
            if first_line:
                first_line = False
                if raw.startswith(codecs.BOM_UTF8):
                    raw = raw[len(codecs.BOM_UTF8):]
            before_line = character_count
            character_count += len(decoder.decode(raw))
            if line_was_partial:
                character_count += _discard_line_remainder(handle, decoder)
                truncated_by_bytes = truncated_by_bytes or (offset <= total + 1 < offset + max_lines)
                exact_possible = False
            total += 1

            if total < offset:
                exact_possible = False
                continue
            if stopped_showing or len(shown) >= max_lines:
                exact_possible = False
                stopped_showing = True
                continue

            if not shown:
                start_char = before_line

            line = _line_text(raw)
            line_bytes = len(raw)
            if used_bytes + line_bytes > MAX_READ_BYTES:
                if not shown:
                    prefix_lines = _decode_utf8_prefix(raw, MAX_READ_BYTES).splitlines()
                    line = prefix_lines[0] if prefix_lines else ""
                    shown.append(line)
                    source_parts.append(_decode_utf8_prefix(raw, MAX_READ_BYTES))
                    used_bytes = len(line.encode("utf-8"))
                truncated_by_bytes = True
                exact_possible = False
                stopped_showing = True
                continue
            used_bytes += line_bytes
            shown.append(line)
            source_parts.append(
                _decode_utf8_prefix(raw, len(raw))
                if line_was_partial else raw.decode("utf-8", errors="replace")
            )
            if line_was_partial:
                # Never append the next line after discarding this one's tail.
                stopped_showing = True
            if exact_bytes is not None and exact_possible:
                exact_bytes.extend(raw)

    if exact_possible and exact_bytes is not None and len(shown) == total:
        return _ReadWindow(shown, total, exact_bytes.decode("utf-8", errors="replace"), False)
    return _ReadWindow(shown, total, None, truncated_by_bytes, "".join(source_parts), start_char)


def _discard_line_remainder(handle, decoder) -> int:
    count = 0
    while True:
        chunk = handle.readline(MAX_READ_BYTES + 1)
        count += len(decoder.decode(chunk, final=not chunk))
        if not chunk or chunk.endswith((b"\n", b"\r")):
            return count


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
            f"Read exact UTF-8 text (max {MAX_READ_LINES} lines/{MAX_READ_BYTES} bytes). "
            "Line offset/limit is one-based; offset_chars/limit_chars uses zero-based Unicode "
            "code points (not bytes/tokens), excluding BOM. Prefer the returned cursor: "
            "budgets may hide part of a complete result. Do not mix units or restart solely "
            "due to display truncation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read."},
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed). Prefer the returned cursor for continuation.",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read.",
                },
                "offset_chars": {"type": "integer", "minimum": 0,
                    "description": "Zero-based Unicode code point offset, excluding BOM. Prefer the returned cursor; not a byte or line offset. Cannot combine with line windows."},
                "limit_chars": {"type": "integer", "minimum": 1,
                    "description": "Character window size (default 1024). Requires offset_chars."},
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
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "description": "Page size, at most 500."},
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
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "description": "Page size, at most 200. Queries are not snapshots."},
            },
            "required": ["path", "pattern"],
        },
    )


def create_file_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_file_tools(registry)
    register_shell_tools(registry)
    return registry

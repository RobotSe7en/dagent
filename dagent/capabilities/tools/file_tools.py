"""Built-in file tools: read, write, edit, and search."""

from __future__ import annotations

import codecs
import difflib
import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from dagent.capabilities.tools.command_tools import register_command_tools
from dagent.capabilities.tools.registry import ToolRegistry


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
MAX_READ_LINES = 2000
MAX_READ_BYTES = 200_000
MAX_LINE_CHARS = 2000
MAX_EDIT_DIFF_LINES = 50

_RG_UNRESOLVED = object()
_rg_path: object = _RG_UNRESOLVED


def read_file(path: str | Path, offset: int = 1, limit: int | None = None) -> str:
    if offset < 1:
        raise ValueError("offset must be at least 1.")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1.")
    text, _ = _read_utf8(Path(path))
    lines = text.splitlines()
    total = len(lines)
    if offset > 1 and offset > total:
        raise ValueError(f"offset {offset} is beyond end of file ({total} lines).")

    max_lines = min(limit, MAX_READ_LINES) if limit is not None else MAX_READ_LINES
    selected = lines[offset - 1 : offset - 1 + max_lines]
    shown: list[str] = []
    used_bytes = 0
    for line in selected:
        if len(line) > MAX_LINE_CHARS:
            line = f"{line[:MAX_LINE_CHARS]} [LINE TRUNCATED]"
        line_bytes = len(line.encode("utf-8")) + 1
        if shown and used_bytes + line_bytes > MAX_READ_BYTES:
            break
        used_bytes += line_bytes
        shown.append(line)

    if offset == 1 and shown == lines:
        return text
    end_line = offset - 1 + len(shown)
    content = "\n".join(shown)
    if end_line < total:
        content += (
            f"\n[TRUNCATED] showing lines {offset}-{end_line} of {total}; "
            "use offset/limit to read more."
        )
    return content


def write_file(path: str | Path, content: str) -> str:
    resolved = Path(path)
    data = content.encode("utf-8")
    _atomic_write(resolved, data)
    return f"Wrote {len(data)} bytes to {resolved}."


def edit_file(path: str | Path, old_string: str, new_string: str) -> str:
    if not old_string:
        raise ValueError("old_string must not be empty.")
    if old_string == new_string:
        raise ValueError("old_string and new_string are identical.")
    resolved = Path(path)
    text, had_bom = _read_utf8(resolved)
    uses_crlf = "\r\n" in text
    normalized = text.replace("\r\n", "\n")
    old = old_string.replace("\r\n", "\n")
    new = new_string.replace("\r\n", "\n")

    count = normalized.count(old)
    if count == 0:
        raise ValueError(
            f"old_string was not found in {resolved}. Read the file and copy the exact text."
        )
    if count > 1:
        raise ValueError(
            f"old_string matched {count} locations in {resolved}; "
            "include more surrounding context to make it unique."
        )

    first_line = normalized.count("\n", 0, normalized.index(old)) + 1
    updated = normalized.replace(old, new, 1)
    output = updated.replace("\n", "\r\n") if uses_crlf else updated
    data = output.encode("utf-8")
    if had_bom:
        data = codecs.BOM_UTF8 + data
    _atomic_write(resolved, data)

    summary = f"Edited {resolved}: 1 replacement at line {first_line}."
    diff = _unified_diff_excerpt(normalized, updated, path=resolved)
    return f"{summary}\n{diff}" if diff else summary


def grep(path: str | Path, pattern: str, glob: str | None = None) -> str:
    root = Path(path)
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
        "--sort",
        "path",
    ]
    for excluded in sorted(GREP_EXCLUDED_DIRS):
        args.extend(["--glob", f"!{excluded}"])
    if glob:
        args.extend(["--glob", glob])
    args.extend(["--regexp", pattern, str(root)])
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=GREP_TIMEOUT_SECONDS,
    )
    if result.returncode == 1:
        return ""
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise ValueError(f"ripgrep failed: {detail}")
    return _capped_matches(result.stdout.splitlines())


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
            f"[TRUNCATED] grep stopped after {GREP_MAX_MATCHES} matches.",
        ]
    return "\n".join(lines)


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
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


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
        diff_lines = [*diff_lines[:MAX_EDIT_DIFF_LINES], f"[DIFF TRUNCATED: {omitted} more lines]"]
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
    register_command_tools(registry)
    return registry

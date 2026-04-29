"""Basic file tools for Milestone 2."""

from __future__ import annotations

import re
from pathlib import Path

from dagent.tools.registry import ToolRegistry


def read_file(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def write_file(path: str | Path, content: str) -> str:
    resolved_path = Path(path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(content, encoding="utf-8")
    return f"Wrote {resolved_path}."


def grep(path: str | Path, pattern: str) -> str:
    root = Path(path)
    matcher = re.compile(pattern)
    files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
    matches: list[str] = []

    for file_path in files:
        try:
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if matcher.search(line):
                matches.append(f"{file_path}:{line_number}:{line}")

    return "\n".join(matches)


def register_file_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="read_file",
        handler=read_file,
        action="read",
        path_args=("path",),
        description="Read a UTF-8 text file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read."},
            },
            "required": ["path"],
        },
    )
    registry.register(
        name="write_file",
        handler=write_file,
        action="write",
        path_args=("path",),
        description="Write UTF-8 text to a file.",
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
        name="grep",
        handler=grep,
        action="read",
        path_args=("path",),
        description="Search text files for a regular expression.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory path."},
                "pattern": {"type": "string", "description": "Regular expression."},
            },
            "required": ["path", "pattern"],
        },
    )


def create_file_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_file_tools(registry)
    return registry

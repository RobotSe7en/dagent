from pathlib import Path

import pytest

from dagent.schemas import Boundary
from dagent.tools.boundary import BoundaryViolation
from dagent.tools.executor import ToolExecutionError, ToolExecutor
from dagent.tools.file_tools import create_file_tool_registry


def make_executor(tmp_path: Path) -> ToolExecutor:
    return ToolExecutor(create_file_tool_registry(), workspace_root=tmp_path)


def test_read_file_reads_allowed_path(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = executor.execute(
        "read_file",
        {"path": "notes.txt"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result == "hello\nworld\n"


def test_read_only_node_cannot_write_file(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    with pytest.raises(BoundaryViolation, match="read_only"):
        executor.execute(
            "write_file",
            {"path": "notes.txt", "content": "nope"},
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
        )


def test_write_limited_node_can_write_allowed_path(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = executor.execute(
        "write_file",
        {"path": "notes.txt", "content": "saved"},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert "Wrote" in result
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "saved"


def test_allowed_paths_prevent_reading_outside_boundary(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    (blocked / "secret.txt").write_text("secret", encoding="utf-8")
    executor = make_executor(tmp_path)

    with pytest.raises(BoundaryViolation, match="outside allowed paths"):
        executor.execute(
            "read_file",
            {"path": "blocked/secret.txt"},
            boundary=Boundary(mode="read_only", allowed_paths=["allowed"]),
        )


def test_relative_parent_path_cannot_escape_allowed_path(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    (blocked / "secret.txt").write_text("secret", encoding="utf-8")
    executor = make_executor(tmp_path)

    with pytest.raises(BoundaryViolation, match="outside allowed paths"):
        executor.execute(
            "read_file",
            {"path": "allowed/../blocked/secret.txt"},
            boundary=Boundary(mode="read_only", allowed_paths=["allowed"]),
        )


def test_absolute_path_cannot_escape_allowed_path(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    secret = blocked / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    executor = make_executor(tmp_path)

    with pytest.raises(BoundaryViolation, match="outside allowed paths"):
        executor.execute(
            "read_file",
            {"path": secret},
            boundary=Boundary(mode="read_only", allowed_paths=["allowed"]),
        )


def test_forbidden_tools_are_blocked(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    executor = make_executor(tmp_path)

    with pytest.raises(BoundaryViolation, match="forbidden"):
        executor.execute(
            "read_file",
            {"path": "notes.txt"},
            boundary=Boundary(mode="read_only", forbidden_tools=["read_file"]),
        )


def test_unregistered_tool_reports_error(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    with pytest.raises(ToolExecutionError, match="not registered"):
        executor.execute(
            "missing_tool",
            {},
            boundary=Boundary(mode="read_only"),
        )


def test_grep_searches_allowed_files(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("gamma\nalphabet\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = executor.execute(
        "grep",
        {"path": ".", "pattern": "alpha"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert "a.txt:1:alpha" in result
    assert "b.txt:2:alphabet" in result

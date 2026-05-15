from pathlib import Path
import sys

import pytest

from dagent.capabilities import CapabilityCatalog
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.harness_runtime import CapabilityExecutionError, CapabilityExecutor
from dagent.schemas import Boundary
from dagent.schemas import CapabilityInvocation, CapabilityResult
from dagent.tools.command_tools import CommandExecutionError, run_command
from dagent.tools.file_tools import create_file_tool_registry


def make_executor(tmp_path: Path) -> CapabilityExecutor:
    registry = CapabilityCatalog(workspace_root=tmp_path)
    executor = CapabilityExecutor(registry)
    ToolCapabilityProvider(create_file_tool_registry()).register_into(registry)
    return executor


def execute(
    executor: CapabilityExecutor,
    tool_name: str,
    args: dict,
    *,
    boundary: Boundary,
) -> CapabilityResult:
    return executor.execute(
        CapabilityInvocation(
            capability_id=f"tool.{tool_name}",
            kind="tool",
            arguments=args,
            boundary=boundary,
        )
    )


def test_read_file_reads_allowed_path(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "read_file",
        {"path": "notes.txt"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.status == "completed"
    assert result.content == "hello\nworld\n"


def test_read_only_node_cannot_write_file(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "write_file",
        {"path": "notes.txt", "content": "nope"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "read_only" in (result.error or "")


def test_write_limited_node_can_write_allowed_path(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "write_file",
        {"path": "notes.txt", "content": "saved"},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert result.status == "completed"
    assert "Wrote" in result.content
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "saved"


def test_allowed_paths_prevent_reading_outside_boundary(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    (blocked / "secret.txt").write_text("secret", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "read_file",
        {"path": "blocked/secret.txt"},
        boundary=Boundary(mode="read_only", allowed_paths=["allowed"]),
    )

    assert result.status == "failed"
    assert "outside allowed paths" in (result.error or "")


def test_relative_parent_path_cannot_escape_allowed_path(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    (blocked / "secret.txt").write_text("secret", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "read_file",
        {"path": "allowed/../blocked/secret.txt"},
        boundary=Boundary(mode="read_only", allowed_paths=["allowed"]),
    )

    assert result.status == "failed"
    assert "outside allowed paths" in (result.error or "")


def test_absolute_path_cannot_escape_allowed_path(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    secret = blocked / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "read_file",
        {"path": secret},
        boundary=Boundary(mode="read_only", allowed_paths=["allowed"]),
    )

    assert result.status == "failed"
    assert "outside allowed paths" in (result.error or "")


def test_unregistered_tool_reports_error(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    with pytest.raises(CapabilityExecutionError, match="not registered"):
        execute(
            executor,
            "missing_tool",
            {},
            boundary=Boundary(mode="read_only"),
        )


def test_grep_searches_allowed_files(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("gamma\nalphabet\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "grep",
        {"path": ".", "pattern": "alpha"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert "a.txt:1:alpha" in result.content
    assert "b.txt:2:alphabet" in result.content


def test_grep_skips_heavy_generated_directories(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "hidden.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "hidden.txt").write_text("alpha\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "grep",
        {"path": ".", "pattern": "alpha"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert "src" in result.content
    assert ".venv" not in result.content
    assert "node_modules" not in result.content


def test_run_command_executes_allowed_command_in_allowed_cwd(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "run_command",
        {"command": "echo hello", "cwd": "."},
        boundary=Boundary(
            mode="write_limited",
            allowed_paths=["."],
            allowed_commands=["echo"],
        ),
    )

    assert "exit_code=0" in result.content
    assert "hello" in result.content


def test_run_command_raises_when_process_exits_nonzero(tmp_path: Path) -> None:
    command = f'"{sys.executable}" -c "raise SystemExit(3)"'

    with pytest.raises(CommandExecutionError, match="exit_code=3"):
        run_command(command, cwd=tmp_path)


def test_read_only_node_can_run_explicitly_allowed_command(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "run_command",
        {"command": "echo hello", "cwd": "."},
        boundary=Boundary(
            mode="read_only",
            allowed_paths=["."],
            allowed_commands=["echo"],
        ),
    )

    assert "exit_code=0" in result.content
    assert "hello" in result.content


def test_run_command_requires_allowed_commands(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "run_command",
        {"command": "echo hello", "cwd": "."},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "allowed_commands" in (result.error or "")


def test_read_only_run_command_uses_default_allowed_inspection_commands(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "run_command",
        {"command": "dir"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert "exit_code=0" in result.content
    assert "notes.txt" in result.content


def test_read_only_run_command_blocks_non_default_command_without_allowlist(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "run_command",
        {"command": "python --version"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "outside allowed commands" in (result.error or "")


def test_run_command_defaults_cwd_to_workspace_root(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "run_command",
        {"command": "echo hello"},
        boundary=Boundary(
            mode="read_only",
            allowed_paths=["."],
            allowed_commands=["echo"],
        ),
    )

    assert "exit_code=0" in result.content
    assert "hello" in result.content


def test_run_command_rejects_shell_control_operators(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "run_command",
        {"command": "echo hello && echo nope", "cwd": "."},
        boundary=Boundary(
            mode="write_limited",
            allowed_paths=["."],
            allowed_commands=["echo"],
        ),
    )

    assert result.status == "failed"
    assert "control operators" in (result.error or "")


def test_run_command_cwd_must_stay_in_allowed_paths(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "run_command",
        {"command": "echo hello", "cwd": "blocked"},
        boundary=Boundary(
            mode="write_limited",
            allowed_paths=["allowed"],
            allowed_commands=["echo"],
        ),
    )

    assert result.status == "failed"
    assert "outside allowed paths" in (result.error or "")

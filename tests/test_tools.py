import asyncio
import io
import os
from pathlib import Path
import stat
import sys
import time

import pytest

from dagent.capabilities import CapabilityCatalog
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.harness_runtime import CapabilityExecutionError, CapabilityExecutor
from dagent.schemas import Boundary
from dagent.schemas import CapabilityInvocation, CapabilityResult
from dagent.capabilities.tools.shell_tools import ShellExecutionError, shell
from dagent.capabilities.tools.file_tools import create_file_tool_registry


def run(coro):
    return asyncio.run(coro)


def make_executor(tmp_path: Path) -> CapabilityExecutor:
    registry = CapabilityCatalog(workspace_root=tmp_path)
    executor = CapabilityExecutor(registry)
    ToolCapabilityProvider(create_file_tool_registry()).register_into(registry)
    return executor


def test_file_tool_registry_does_not_expose_internal_dag_start() -> None:
    registry = create_file_tool_registry()

    assert "dag_start" not in registry.names()


def execute(
    executor: CapabilityExecutor,
    tool_name: str,
    args: dict,
    *,
    boundary: Boundary,
) -> CapabilityResult:
    return run(executor.execute(
        CapabilityInvocation(
            capability_id=f"tool.{tool_name}",
            kind="tool",
            arguments=args,
            boundary=boundary,
        )
    ))


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


def test_shell_executes_command_in_allowed_cwd(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "shell",
        {"command": "echo hello", "cwd": "."},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert "exit_code=0" in result.content
    assert "hello" in result.content


def test_shell_raises_when_process_exits_nonzero(tmp_path: Path) -> None:
    command = f'"{sys.executable}" -c "raise SystemExit(3)"'

    with pytest.raises(ShellExecutionError, match="exit_code=3"):
        shell(command, cwd=tmp_path)


def test_shell_blocks_blacklisted_command(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "shell",
        {"command": "rm -rf /", "cwd": "."},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "blocked by shell safety policy" in (result.error or "")


def test_shell_defaults_cwd_to_workspace_root(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "shell",
        {"command": "echo hello"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert "exit_code=0" in result.content
    assert "hello" in result.content


def test_shell_blocks_sudo_password_stdin(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "shell",
        {"command": "echo password | sudo -S whoami", "cwd": "."},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "blocked by shell safety policy" in (result.error or "")


def test_shell_cwd_must_stay_in_allowed_paths(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "shell",
        {"command": "echo hello", "cwd": "blocked"},
        boundary=Boundary(mode="write_limited", allowed_paths=["allowed"]),
    )

    assert result.status == "failed"
    assert "outside allowed paths" in (result.error or "")


def test_shell_option_paths_must_stay_in_allowed_paths(tmp_path: Path) -> None:
    from dagent.capabilities.tools.boundary import BoundaryViolation, enforce_command_paths_allowed

    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()

    for command in (
        "tool --output=../blocked/out.txt",
        "tool --config=/tmp/outside.toml",
        "tool --config ../blocked/config.toml",
        "tool < ../blocked/in.txt",
        "tool > ../blocked/out.txt",
    ):
        with pytest.raises(BoundaryViolation, match="resolves outside allowed paths"):
            enforce_command_paths_allowed(
                command,
                Boundary(mode="write_limited", allowed_paths=["allowed"]),
                tmp_path,
                "allowed",
            )


def test_shell_absolute_executable_path_is_not_treated_as_file_argument(tmp_path: Path) -> None:
    from dagent.capabilities.tools.boundary import enforce_command_paths_allowed

    allowed = tmp_path / "allowed"
    allowed.mkdir()

    enforce_command_paths_allowed(
        "/bin/echo hello",
        Boundary(mode="write_limited", allowed_paths=["allowed"]),
        tmp_path,
        "allowed",
    )


# ---------------------------------------------------------------------------
# read_file paging and limits
# ---------------------------------------------------------------------------


def test_read_file_offset_and_limit_slice_with_truncation_footer(tmp_path: Path) -> None:
    target = tmp_path / "lines.txt"
    target.write_text("".join(f"line{i}\n" for i in range(1, 11)), encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "read_file",
        {"path": "lines.txt", "offset": 3, "limit": 2},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.status == "completed"
    assert result.content.startswith("line3\nline4")
    assert "[TRUNCATED] showing lines 3-4 of 10" in result.content


def test_read_file_full_read_preserves_exact_content(tmp_path: Path) -> None:
    target = tmp_path / "exact.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "read_file",
        {"path": "exact.txt"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.content == "alpha\nbeta\n"


def test_read_file_keeps_long_lines_exact_for_edit_workflow(tmp_path: Path) -> None:
    long_line = "a" * 2500
    target = tmp_path / "lock.txt"
    target.write_text(f"{long_line}\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    read_result = execute(
        executor,
        "read_file",
        {"path": "lock.txt"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )
    edit_result = execute(
        executor,
        "edit_file",
        {"path": "lock.txt", "old_string": long_line, "new_string": "short"},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert read_result.content == f"{long_line}\n"
    assert "LINE TRUNCATED" not in read_result.content
    assert edit_result.status == "completed"
    assert target.read_text(encoding="utf-8") == "short\n"


def test_read_file_null_offset_fails_instead_of_using_schema_default(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "read_file",
        {"path": "notes.txt", "offset": None},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "offset must be an integer" in (result.error or "")


def test_read_file_offset_beyond_end_of_file_fails(tmp_path: Path) -> None:
    (tmp_path / "short.txt").write_text("one\ntwo\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "read_file",
        {"path": "short.txt", "offset": 99},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "beyond end of file" in (result.error or "")


def test_read_file_rejects_binary_content(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02data")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "read_file",
        {"path": "blob.bin"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "not a UTF-8 text file" in (result.error or "")


def test_read_file_caps_default_read_at_max_lines(tmp_path: Path) -> None:
    from dagent.capabilities.tools.file_tools import MAX_READ_LINES

    total = MAX_READ_LINES + 5
    (tmp_path / "big.txt").write_text("".join(f"{i}\n" for i in range(total)), encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "read_file",
        {"path": "big.txt"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert f"showing lines 1-{MAX_READ_LINES} of {total}" in result.content


def test_read_file_marks_oversized_single_line_as_truncated(tmp_path: Path) -> None:
    from dagent.capabilities.tools.file_tools import MAX_READ_BYTES

    target = tmp_path / "single-line.txt"
    target.write_text("a" * (MAX_READ_BYTES + 10), encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "read_file",
        {"path": "single-line.txt"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.status == "completed"
    assert result.content.startswith("a" * 100)
    assert "[TRUNCATED]" in result.content
    assert "showing lines 1-1 of 1" in result.content


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


def test_edit_file_replaces_unique_match_and_reports_diff(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("def main():\n    return 1\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "edit_file",
        {"path": "app.py", "old_string": "    return 1", "new_string": "    return 2"},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert result.status == "completed"
    assert "1 replacement at line 2" in result.content
    assert "-    return 1" in result.content
    assert "+    return 2" in result.content
    assert target.read_text(encoding="utf-8") == "def main():\n    return 2\n"


def test_edit_file_fails_when_old_string_not_found(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "edit_file",
        {"path": "app.py", "old_string": "missing text", "new_string": "x"},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "was not found" in (result.error or "")


def test_edit_file_fails_when_old_string_is_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "edit_file",
        {"path": "app.py", "old_string": "x = 1", "new_string": "x = 2"},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "matched 2 locations" in (result.error or "")
    assert "more surrounding context" in (result.error or "")


def test_edit_file_preserves_crlf_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "win.txt"
    target.write_bytes(b"alpha\r\nbeta\r\n")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "edit_file",
        {"path": "win.txt", "old_string": "beta", "new_string": "gamma"},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert result.status == "completed"
    assert target.read_bytes() == b"alpha\r\ngamma\r\n"


def test_edit_file_preserves_mixed_line_endings_exactly(tmp_path: Path) -> None:
    target = tmp_path / "mixed.txt"
    target.write_bytes(b"alpha\r\nbeta\ngamma\r\n")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "edit_file",
        {"path": "mixed.txt", "old_string": "beta", "new_string": "BETA"},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert result.status == "completed"
    assert target.read_bytes() == b"alpha\r\nBETA\ngamma\r\n"


def test_edit_file_requires_exact_newline_match(tmp_path: Path) -> None:
    target = tmp_path / "win.txt"
    target.write_bytes(b"alpha\r\nbeta\r\n")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "edit_file",
        {"path": "win.txt", "old_string": "alpha\nbeta", "new_string": "changed"},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "was not found" in (result.error or "")
    assert target.read_bytes() == b"alpha\r\nbeta\r\n"


def test_concurrent_edit_file_operations_on_same_path_do_not_lose_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dagent.capabilities.tools import file_tools

    target = tmp_path / "state.txt"
    target.write_text("alpha=1\nbeta=1\n", encoding="utf-8")
    executor = make_executor(tmp_path)
    original_read_utf8 = file_tools._read_utf8

    def slow_read_utf8(path: Path) -> tuple[str, bool]:
        text, had_bom = original_read_utf8(path)
        time.sleep(0.05)
        return text, had_bom

    monkeypatch.setattr(file_tools, "_read_utf8", slow_read_utf8)

    async def edit(args: dict[str, str]) -> CapabilityResult:
        return await executor.execute(
            CapabilityInvocation(
                capability_id="tool.edit_file",
                kind="tool",
                arguments={"path": "state.txt", **args},
                boundary=Boundary(mode="write_limited", allowed_paths=["."]),
            )
        )

    async def run_edits() -> tuple[CapabilityResult, CapabilityResult]:
        first_result, second_result = await asyncio.gather(
            edit({"old_string": "alpha=1", "new_string": "alpha=2"}),
            edit({"old_string": "beta=1", "new_string": "beta=2"}),
        )
        return first_result, second_result

    first, second = run(run_edits())

    assert first.status == "completed"
    assert second.status == "completed"
    assert target.read_text(encoding="utf-8") == "alpha=2\nbeta=2\n"


def test_edit_file_preserves_utf8_bom(tmp_path: Path) -> None:
    target = tmp_path / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbfhello\n")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "edit_file",
        {"path": "bom.txt", "old_string": "hello", "new_string": "world"},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert result.status == "completed"
    assert target.read_bytes() == b"\xef\xbb\xbfworld\n"


def test_read_only_node_cannot_edit_file(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "edit_file",
        {"path": "app.py", "old_string": "x = 1", "new_string": "x = 2"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "read_only" in (result.error or "")


def test_edit_file_rejects_identical_strings(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "edit_file",
        {"path": "app.py", "old_string": "x = 1", "new_string": "x = 1"},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "identical" in (result.error or "")


def test_write_file_reports_bytes_written(tmp_path: Path) -> None:
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "write_file",
        {"path": "out.txt", "content": "hello"},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert "Wrote 5 bytes" in result.content


def test_write_file_uses_umask_preserves_mode_and_detaches_hardlinks(tmp_path: Path) -> None:
    if not hasattr(os, "link"):
        pytest.skip("hard links are not available on this platform")
    executor = make_executor(tmp_path)
    old_umask = os.umask(0o022)
    try:
        result = execute(
            executor,
            "write_file",
            {"path": "new.txt", "content": "hello"},
            boundary=Boundary(mode="write_limited", allowed_paths=["."]),
        )
    finally:
        os.umask(old_umask)

    assert result.status == "completed"
    assert stat.S_IMODE((tmp_path / "new.txt").stat().st_mode) == 0o644

    existing = tmp_path / "existing.txt"
    existing.write_text("old", encoding="utf-8")
    os.chmod(existing, 0o640)
    linked = tmp_path / "linked.txt"
    os.link(existing, linked)

    result = execute(
        executor,
        "write_file",
        {"path": "existing.txt", "content": "new"},
        boundary=Boundary(mode="write_limited", allowed_paths=["."]),
    )

    assert result.status == "completed"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o640
    assert existing.read_text(encoding="utf-8") == "new"
    assert linked.read_text(encoding="utf-8") == "old"
    assert existing.stat().st_ino != linked.stat().st_ino


def test_write_file_does_not_mutate_hardlinked_file_outside_boundary(tmp_path: Path) -> None:
    if not hasattr(os, "link"):
        pytest.skip("hard links are not available on this platform")
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    allowed_link = allowed / "shared.txt"
    blocked_link = blocked / "shared.txt"
    allowed_link.write_text("old", encoding="utf-8")
    os.link(allowed_link, blocked_link)
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "write_file",
        {"path": "allowed/shared.txt", "content": "new"},
        boundary=Boundary(mode="write_limited", allowed_paths=["allowed"]),
    )

    assert result.status == "completed"
    assert allowed_link.read_text(encoding="utf-8") == "new"
    assert blocked_link.read_text(encoding="utf-8") == "old"
    assert allowed_link.stat().st_ino != blocked_link.stat().st_ino


# ---------------------------------------------------------------------------
# grep backends
# ---------------------------------------------------------------------------


def _force_python_grep(monkeypatch: pytest.MonkeyPatch) -> None:
    from dagent.capabilities.tools import file_tools

    monkeypatch.setattr(file_tools, "_rg_path", None)


def test_grep_python_fallback_matches_and_filters_by_glob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_python_grep(monkeypatch)
    (tmp_path / "a.py").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "grep",
        {"path": ".", "pattern": "alpha", "glob": "*.py"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert "a.py:1:alpha" in result.content
    assert "a.txt" not in result.content


@pytest.mark.skipif(__import__("shutil").which("rg") is None, reason="ripgrep not installed")
def test_grep_ripgrep_backend_matches_and_filters_by_glob(tmp_path: Path) -> None:
    from dagent.capabilities.tools.file_tools import _grep_with_ripgrep
    import shutil as _shutil

    (tmp_path / "a.py").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")

    content = _grep_with_ripgrep(_shutil.which("rg"), tmp_path, "alpha", "*.py")

    assert "a.py:1:alpha" in content
    assert "a.txt" not in content


@pytest.mark.skipif(__import__("shutil").which("rg") is None, reason="ripgrep not installed")
def test_grep_backends_agree_on_simple_search(tmp_path: Path) -> None:
    from dagent.capabilities.tools.file_tools import _grep_pure_python, _grep_with_ripgrep
    import shutil as _shutil

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "one.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "src" / "two.txt").write_text("alphabet\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skip.txt").write_text("alpha\n", encoding="utf-8")

    rg_output = _grep_with_ripgrep(_shutil.which("rg"), tmp_path, "alpha", None)
    py_output = _grep_pure_python(tmp_path, "alpha", None)

    assert rg_output.splitlines() == py_output.splitlines()


@pytest.mark.skipif(__import__("shutil").which("rg") is None, reason="ripgrep not installed")
def test_grep_backends_agree_on_python_regex_and_ignore_files(tmp_path: Path) -> None:
    from dagent.capabilities.tools.file_tools import _grep_pure_python, _grep_with_ripgrep
    import shutil as _shutil

    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("alphabet\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("alphabet\n", encoding="utf-8")

    rg_output = _grep_with_ripgrep(_shutil.which("rg"), tmp_path, "alpha(?=bet)", None)
    py_output = _grep_pure_python(tmp_path, "alpha(?=bet)", None)

    assert rg_output.splitlines() == py_output.splitlines()


def test_grep_ripgrep_backend_streams_and_stops_after_match_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dagent.capabilities.tools import file_tools
    from dagent.capabilities.tools.file_tools import GREP_MAX_MATCHES, _grep_with_ripgrep

    class FakeStdout:
        def __init__(self) -> None:
            self.index = 0
            self.closed = False
            self.lines = [
                f"{tmp_path / 'many.txt'}:{index}:alpha\n"
                for index in range(1, GREP_MAX_MATCHES + 25)
            ]

        def __iter__(self):
            return self

        def __next__(self) -> str:
            if self.closed or self.index >= len(self.lines):
                raise StopIteration
            line = self.lines[self.index]
            self.index += 1
            return line

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = FakeStdout()
            self.stderr = io.StringIO("")
            self.returncode: int | None = None
            self.terminated = False
            self.killed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            self.stdout.close()

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15
            self.stdout.close()

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.stdout.close()

        def wait(self, timeout: float | None = None) -> int:
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    processes: list[FakeProcess] = []

    def fake_popen(*args, **kwargs) -> FakeProcess:
        process = FakeProcess()
        processes.append(process)
        return process

    def fail_if_capture_output_is_used(*args, **kwargs):
        pytest.fail("ripgrep output must be streamed instead of captured in full")

    monkeypatch.setattr(file_tools.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(file_tools.subprocess, "run", fail_if_capture_output_is_used)

    output = _grep_with_ripgrep("rg", tmp_path, "alpha", None)

    assert len(processes) == 1
    assert processes[0].terminated
    assert not processes[0].killed
    assert f"{tmp_path / 'many.txt'}:{GREP_MAX_MATCHES}:alpha" in output
    assert f"{tmp_path / 'many.txt'}:{GREP_MAX_MATCHES + 1}:alpha" not in output
    assert "[TRUNCATED] grep stopped after" in output


# ---------------------------------------------------------------------------
# shell hardening
# ---------------------------------------------------------------------------


def test_shell_fails_when_cwd_does_not_exist(tmp_path: Path) -> None:
    with pytest.raises(ShellExecutionError, match="Working directory does not exist"):
        shell("echo hi", cwd=tmp_path / "missing")


def test_shell_keeps_tail_of_oversized_output(tmp_path: Path) -> None:
    from dagent.capabilities.tools.shell_tools import SHELL_OUTPUT_MAX_LINES

    total = SHELL_OUTPUT_MAX_LINES + 50
    command = f'"{sys.executable}" -c "[print(f\'line{{i}}\') for i in range({total})]"'

    output = shell(command, cwd=tmp_path)

    assert "[TRUNCATED] output exceeded limits; showing tail" in output
    assert f"line{total - 1}" in output
    assert "line0" not in output


def test_shell_byte_truncation_stays_under_limit_and_preserves_utf8() -> None:
    from dagent.capabilities.tools.shell_tools import SHELL_OUTPUT_MAX_BYTES, _tail_truncate

    output = _tail_truncate("€" * 40_000)

    assert len(output.encode("utf-8")) <= SHELL_OUTPUT_MAX_BYTES
    assert "\ufffd" not in output


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


def test_list_files_lists_dirs_and_files_with_structured_value(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "skip.txt").write_text("x", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "list_files",
        {"path": "."},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.status == "completed"
    assert f"{tmp_path / 'src'}/" in result.content
    assert str(tmp_path / "src" / "main.py") in result.content
    assert ".venv" not in result.content
    assert result.value == result.content.splitlines()


def test_list_files_depth_limits_recursion(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "deep.txt").write_text("x", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "list_files",
        {"path": ".", "depth": 2},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert f"{tmp_path / 'a' / 'b'}/" in result.content
    assert "deep.txt" not in result.content


def test_list_files_null_depth_fails_instead_of_using_schema_default(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "a" / "b" / "deep.txt").write_text("x", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "list_files",
        {"path": ".", "depth": None},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "depth must be an integer" in (result.error or "")


def test_list_files_glob_lists_matching_files_only(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "notes.txt").write_text("x", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "list_files",
        {"path": ".", "glob": "*.py"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.value == [str(tmp_path / "src" / "main.py")]
    assert "notes.txt" not in result.content
    assert not any(entry.endswith("/") for entry in result.value)


def test_list_files_caps_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dagent.capabilities.tools import file_tools

    monkeypatch.setattr(file_tools, "LIST_MAX_ENTRIES", 3)
    for index in range(5):
        (tmp_path / f"f{index}.txt").write_text("x", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "list_files",
        {"path": "."},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert "[TRUNCATED] showing first 3 entries; more entries exist." in result.content
    assert len(result.value) == 3


def test_list_files_rejects_paths_outside_boundary(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "list_files",
        {"path": "blocked"},
        boundary=Boundary(mode="read_only", allowed_paths=["allowed"]),
    )

    assert result.status == "failed"
    assert "outside allowed paths" in (result.error or "")


def test_list_files_fails_on_non_directory(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    executor = make_executor(tmp_path)

    result = execute(
        executor,
        "list_files",
        {"path": "file.txt"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )

    assert result.status == "failed"
    assert "is not a directory" in (result.error or "")

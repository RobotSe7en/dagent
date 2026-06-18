"""Tests for the Docker sandbox execution path.

These tests do not require a running Docker daemon: the docker SDK is faked, and
the in-container worker is exercised as a subprocess to prove it runs the real
tool implementations (anti-drift).
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from dagent.capabilities.bootstrap import create_default_capability_catalog
from dagent.capabilities.sandbox import (
    SandboxExecutionError,
    SandboxSession,
    SandboxToolExecutionError,
    build_skeleton,
    sandbox_status,
)
from dagent.capabilities.sandbox_context import (
    run_execution_context,
    sandbox_session_context,
)
from dagent.capabilities.tools.file_tools import create_file_tool_registry
from dagent.capabilities.tools.registry import ToolOutput
from dagent.harness_runtime.capability_executor import CapabilityExecutor
from dagent.runner import _resolve_run_execution
from dagent.schemas import Boundary, CapabilityInvocation, DockerSandboxConfig, RunState
import dagent.capabilities.sandbox as sandbox_module


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Skeleton + worker (anti-drift): real tool code, no Docker
# --------------------------------------------------------------------------

def test_build_skeleton_copies_real_modules_without_boundary(tmp_path: Path):
    root = build_skeleton(tmp_path)
    tools = root / "dagent" / "capabilities" / "tools"
    for name in ("registry.py", "file_tools.py", "shell_tools.py", "sandbox_worker.py"):
        assert (tools / name).is_file()
    for pkg in (root / "dagent", root / "dagent" / "capabilities", tools):
        assert (pkg / "__init__.py").read_text() == ""
    # boundary.py must never be shipped (it pulls pydantic via dagent.schemas)
    assert not (tools / "boundary.py").exists()


def _run_worker(skeleton_root: Path, tool: str, args: dict) -> dict:
    request = base64.b64encode(json.dumps({"tool": tool, "args": args}).encode()).decode()
    worker = skeleton_root / "dagent/capabilities/tools/sandbox_worker.py"
    proc = subprocess.run(
        [sys.executable, str(worker), request],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(skeleton_root)},
        cwd="/tmp",
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_worker_matches_local_tools(tmp_path: Path):
    skeleton = build_skeleton(tmp_path / "skel")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "a.txt"

    # write via worker, read back via local handler -> identical content
    written = _run_worker(skeleton, "write_file", {"path": str(target), "content": "hello\nworld\n"})
    assert written["ok"] is True
    local = create_file_tool_registry().get("read_file").handler(path=str(target))
    worker_read = _run_worker(skeleton, "read_file", {"path": str(target)})
    assert worker_read["ok"] is True
    assert worker_read["content"] == local

    # list_files structured value round-trips
    listed = _run_worker(skeleton, "list_files", {"path": str(workspace)})
    assert listed["ok"] is True
    assert listed["value"] == [str(target)]


def test_worker_reports_structured_error(tmp_path: Path):
    skeleton = build_skeleton(tmp_path)
    result = _run_worker(skeleton, "read_file", {"path": str(tmp_path / "missing")})
    assert result["ok"] is False
    assert result["error_type"] == "FileNotFoundError"


# --------------------------------------------------------------------------
# Fake docker SDK
# --------------------------------------------------------------------------

class _FakeImageNotFound(Exception):
    pass


class _FakeErrors:
    ImageNotFound = _FakeImageNotFound


class _FakeImages:
    def __init__(self, present: set[str]):
        self.present = set(present)
        self.pulled: list[str] = []

    def get(self, image: str):
        if image not in self.present:
            raise _FakeImageNotFound(image)
        return object()

    def pull(self, image: str):
        self.pulled.append(image)
        self.present.add(image)


class _FakeContainer:
    def __init__(self, exec_result):
        self._exec_result = exec_result
        self.exec_calls: list[list[str]] = []
        self.stopped = False
        self.removed = False

    def exec_run(self, cmd, user=None, demux=False):
        self.exec_calls.append(cmd)
        return self._exec_result

    def stop(self, timeout=None):
        self.stopped = True

    def remove(self, force=False):
        self.removed = True


class _FakeContainers:
    def __init__(self, container, *, start_delay=0.0):
        self.container = container
        self.run_kwargs = None
        self.run_count = 0
        self._start_delay = start_delay

    def run(self, image, command, **kwargs):
        if self._start_delay:
            import time

            time.sleep(self._start_delay)
        self.run_count += 1
        self.run_kwargs = {"image": image, "command": command, **kwargs}
        return self.container


class _FakeClient:
    def __init__(self, *, present, container, start_delay=0.0):
        self.images = _FakeImages(present)
        self.containers = _FakeContainers(container, start_delay=start_delay)

    def ping(self):
        return True


class _FakeDocker:
    errors = _FakeErrors

    def __init__(self, client):
        self._client = client

    def from_env(self):
        return self._client


def _patch_docker(monkeypatch, client):
    monkeypatch.setattr(sandbox_module, "_import_docker", lambda: _FakeDocker(client))


def _ok_exec(content="ok", value=None):
    payload = json.dumps({"ok": True, "content": content, "value": value}).encode()
    return (0, (payload, b""))


# --------------------------------------------------------------------------
# SandboxSession with fake docker
# --------------------------------------------------------------------------

def test_session_pulls_missing_image_and_identity_mounts(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    skill = tmp_path / "skills" / "writing"
    skill.mkdir(parents=True)
    container = _FakeContainer(_ok_exec(content="hi", value=["x"]))
    client = _FakeClient(present=set(), container=container)
    _patch_docker(monkeypatch, client)

    session = SandboxSession(
        DockerSandboxConfig(),
        workspace_root=workspace,
        skill_dirs=(skill,),
    )
    out = session.run_tool("read_file", {"path": workspace / "a.txt"})
    assert isinstance(out, ToolOutput)
    assert out.content == "hi"
    assert out.value == ["x"]

    # image was missing -> pulled
    assert client.images.pulled == [DockerSandboxConfig().image]
    # identity mounts: workspace rw at its own path, skill ro at its own path
    volumes = client.containers.run_kwargs["volumes"]
    assert volumes[str(workspace.resolve())]["bind"] == str(workspace.resolve())
    assert volumes[str(workspace.resolve())]["mode"] == "rw"
    assert volumes[str(skill.resolve())]["mode"] == "ro"
    assert client.containers.run_kwargs["read_only"] is True
    assert client.containers.run_kwargs["network_disabled"] is True
    session.close()
    assert container.stopped and container.removed


def test_session_starts_container_once_under_concurrency(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    container = _FakeContainer(_ok_exec())
    client = _FakeClient(present={DockerSandboxConfig().image}, container=container, start_delay=0.05)
    _patch_docker(monkeypatch, client)
    session = SandboxSession(DockerSandboxConfig(), workspace_root=workspace)

    errors: list[Exception] = []

    def call():
        try:
            session.run_tool("read_file", {"path": workspace / "a.txt"})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert client.containers.run_count == 1
    session.close()


def test_session_maps_tool_error_to_stop_reason(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = json.dumps({"ok": False, "error": "boom", "error_type": "ValueError"}).encode()
    container = _FakeContainer((0, (payload, b"")))
    client = _FakeClient(present={DockerSandboxConfig().image}, container=container)
    _patch_docker(monkeypatch, client)
    session = SandboxSession(DockerSandboxConfig(), workspace_root=workspace)

    with pytest.raises(SandboxToolExecutionError) as excinfo:
        session.run_tool("read_file", {"path": workspace / "a.txt"})
    assert excinfo.value.stop_reason == "ValueError"
    session.close()


def test_session_timeout_exit_code(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    container = _FakeContainer((124, (b"", b"")))
    client = _FakeClient(present={DockerSandboxConfig().image}, container=container)
    _patch_docker(monkeypatch, client)
    session = SandboxSession(DockerSandboxConfig(tool_timeout_seconds=3), workspace_root=workspace)

    with pytest.raises(SandboxExecutionError) as excinfo:
        session.run_tool("shell", {"command": "sleep 999", "cwd": str(workspace)})
    assert "timed out" in str(excinfo.value)
    session.close()


def test_run_tool_serializes_path_arguments(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    container = _FakeContainer(_ok_exec())
    client = _FakeClient(present={DockerSandboxConfig().image}, container=container)
    _patch_docker(monkeypatch, client)
    session = SandboxSession(DockerSandboxConfig(), workspace_root=workspace)
    # Path objects (as produced by boundary resolution) must be JSON-serializable
    session.run_tool("read_file", {"path": workspace / "a.txt"})
    cmd = container.exec_calls[0]
    request = json.loads(base64.b64decode(cmd[-1]).decode())
    assert request["tool"] == "read_file"
    assert request["args"]["path"] == str(workspace / "a.txt")
    session.close()


# --------------------------------------------------------------------------
# Real Docker integration (skipped when no daemon): proves the skeleton
# imports and runs the real tools inside the *configured* image. Catches what
# static tests cannot -- e.g. the configured image missing a dependency the
# skeleton needs to import.
# --------------------------------------------------------------------------

def _docker_live() -> bool:
    try:
        import docker  # requires the [sandbox] extra
    except Exception:
        return False
    try:
        return bool(docker.from_env().ping())
    except Exception:
        return False


@pytest.mark.skipif(not _docker_live(), reason="requires a running Docker daemon")
def test_real_docker_session_runs_tools_in_container(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = SandboxSession(DockerSandboxConfig(), workspace_root=workspace)
    try:
        target = workspace / "a.txt"
        # write_file -> read_file round trip (identity mount: host absolute
        # paths are valid inside the container with no translation)
        wrote = session.run_tool("write_file", {"path": target, "content": "hello\nworld\n"})
        assert isinstance(wrote, ToolOutput)
        read = session.run_tool("read_file", {"path": target})
        assert "hello" in read.content
        # file written in-container is visible on the host (identity mount + host uid:gid)
        assert target.read_text() == "hello\nworld\n"
        # list_files structured value round-trips
        listed = session.run_tool("list_files", {"path": workspace})
        assert listed.value == [str(target.resolve())]
        # shell really executes inside the container (uname reflects the image)
        shell = session.run_tool("shell", {"command": "uname -a"})
        assert "Linux" in shell.content
    finally:
        session.close()


# --------------------------------------------------------------------------
# Routing through the capability handler
# --------------------------------------------------------------------------

class _RecordingSession:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def run_tool(self, tool_name, arguments):
        self.calls.append((tool_name, dict(arguments)))
        return ToolOutput(content="SANDBOXED", value=None)


def _read_file_invocation(mode="read_only"):
    return CapabilityInvocation(
        capability_id="tool.read_file",
        kind="tool",
        arguments={"path": "a.txt"},
        boundary=Boundary(mode=mode, allowed_paths=["."]),
    )


def test_local_execution_runs_handler(tmp_path: Path):
    (tmp_path / "a.txt").write_text("local-content\n")
    catalog = create_default_capability_catalog(workspace_root=tmp_path)
    executor = CapabilityExecutor(catalog)
    result = run(executor.execute(_read_file_invocation()))
    assert result.status == "completed"
    assert "local-content" in result.content


def test_sandbox_execution_routes_to_session(tmp_path: Path):
    (tmp_path / "a.txt").write_text("local-content\n")
    catalog = create_default_capability_catalog(workspace_root=tmp_path)
    executor = CapabilityExecutor(catalog)
    session = _RecordingSession()
    with run_execution_context("sandbox"), sandbox_session_context(session):
        result = run(executor.execute(_read_file_invocation()))
    assert result.content == "SANDBOXED"
    assert len(session.calls) == 1
    tool_name, args = session.calls[0]
    assert tool_name == "read_file"
    # boundary resolved the path to an absolute host path (valid in container via identity mount)
    assert Path(args["path"]) == (tmp_path / "a.txt").resolve()


def test_sandbox_execution_still_enforces_boundary(tmp_path: Path):
    catalog = create_default_capability_catalog(workspace_root=tmp_path)
    executor = CapabilityExecutor(catalog)
    session = _RecordingSession()
    invocation = CapabilityInvocation(
        capability_id="tool.write_file",
        kind="tool",
        arguments={"path": "a.txt", "content": "x"},
        boundary=Boundary(mode="read_only", allowed_paths=["."]),
    )
    with run_execution_context("sandbox"), sandbox_session_context(session):
        result = run(executor.execute(invocation))
    assert result.status == "failed"
    assert session.calls == []  # boundary rejected before reaching the sandbox


# --------------------------------------------------------------------------
# Execution resolution + status
# --------------------------------------------------------------------------

def test_resolve_run_execution_without_state():
    assert _resolve_run_execution("local", None) == "local"
    assert _resolve_run_execution("sandbox", None) == "sandbox"


def test_resolve_run_execution_uses_state():
    state = RunState(run_id="r", kind="tool", status="completed", execution="sandbox")
    assert _resolve_run_execution("local", state) == "sandbox"
    assert _resolve_run_execution("sandbox", state) == "sandbox"


def test_resolve_run_execution_conflict_raises():
    state = RunState(run_id="r", kind="tool", status="completed", execution="local")
    with pytest.raises(ValueError):
        _resolve_run_execution("sandbox", state)


def test_sandbox_status_without_docker(monkeypatch):
    def _raise():
        raise SandboxExecutionError("Docker sandbox requires the 'docker' extra.")

    monkeypatch.setattr(sandbox_module, "_import_docker", _raise)
    status = sandbox_status()
    assert status["docker_available"] is False
    assert "error" in status

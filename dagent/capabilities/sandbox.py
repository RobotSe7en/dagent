"""Docker-backed sandbox engine for built-in tool execution.

Runs the *real* built-in tools inside a per-run, long-lived container. Only the
stdlib-only tool implementation modules are made available in the container (via
a minimal package skeleton mounted read-only), so there is no duplicated tool
logic and the stock ``python`` image needs no dagent install.

The run workspace and visible skill directories are bind-mounted at their
original host paths (identity mount), so host-resolved absolute paths are valid
inside the container with no translation.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from dagent.capabilities.tools.registry import ToolOutput
from dagent.schemas import DockerSandboxConfig, SandboxConfig


_logger = logging.getLogger(__name__)

# Extra seconds the host watchdog allows beyond a tool's own timeout, so an
# in-tool timeout surfaces its clean error before the hard `timeout` kill.
_WATCHDOG_GRACE_SECONDS = 5

SKELETON_BIND = "/opt/dagent-sandbox"
WORKER_IN_CONTAINER = f"{SKELETON_BIND}/dagent/capabilities/tools/sandbox_worker.py"
_SKELETON_MODULES = (
    "dagent.capabilities.tools.registry",
    "dagent.capabilities.tools.file_tools",
    "dagent.capabilities.tools.shell_tools",
    "dagent.capabilities.tools.sandbox_worker",
)


class SandboxExecutionError(RuntimeError):
    """Raised when a sandbox backend cannot execute a tool call."""


class SandboxToolExecutionError(SandboxExecutionError):
    """Raised when the tool inside the sandbox reports failure."""

    def __init__(self, message: str, *, stop_reason: str = "SandboxToolExecutionError") -> None:
        super().__init__(message)
        self.stop_reason = stop_reason


def _import_docker() -> Any:
    try:
        import docker
    except ModuleNotFoundError as exc:
        raise SandboxExecutionError(
            "Docker sandbox requires the 'docker' extra: pip install dagent-ai[sandbox]."
        ) from exc
    return docker


def _default_docker_user(configured_user: str | None) -> str:
    if configured_user:
        return configured_user
    # Match the container user to the host user so files created in the
    # identity-mounted workspace are owned by (and writable to) the host. The
    # Docker daemon is Unix-only, so os.getuid/getgid are always available here.
    # When the host runs as root we fall back to 1000:1000 rather than run the
    # container as root; override via DockerSandboxConfig.user when the
    # workspace is owned by a different uid (e.g. Docker Desktop / rootless).
    uid = os.getuid()
    gid = os.getgid()
    if uid == 0:
        return "1000:1000"
    return f"{uid}:{gid}"


def build_skeleton(root: Path) -> Path:
    """Materialize a minimal importable package with the real tool modules.

    Copies the stdlib-only tool modules (registry/file_tools/shell_tools) and the
    worker into ``<root>/dagent/capabilities/tools/`` with empty ``__init__.py``
    shims at each package level, so the worker can ``import
    dagent.capabilities.tools.file_tools`` on a stock python image without
    dragging the heavy real ``dagent`` package. ``boundary.py`` is never copied.
    """
    tools_dir = root / "dagent" / "capabilities" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    for package_dir in (
        root / "dagent",
        root / "dagent" / "capabilities",
        tools_dir,
    ):
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
    for module in _SKELETON_MODULES:
        spec = importlib.util.find_spec(module)
        if spec is None or spec.origin is None:
            raise SandboxExecutionError(f"Cannot locate source for '{module}'.")
        shutil.copy2(spec.origin, tools_dir / Path(spec.origin).name)
    return root


class SandboxSession:
    """A per-run, lazily-started Docker container reused across tool calls."""

    def __init__(
        self,
        config: DockerSandboxConfig,
        *,
        workspace_root: Path,
        skill_dirs: tuple[Path, ...] = (),
    ) -> None:
        self.config = config
        self.workspace_root = Path(workspace_root).resolve()
        self.skill_dirs = tuple(dict.fromkeys(Path(d).resolve() for d in skill_dirs))
        self._lock = threading.Lock()
        self._container: Any | None = None
        self._skeleton_dir: Path | None = None
        self._closed = False
        self._user = _default_docker_user(config.user)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Eagerly start the container, surfacing daemon errors up front."""
        self._ensure_started()

    def _ensure_started(self) -> Any:
        if self._container is not None:
            return self._container
        with self._lock:
            if self._closed:
                raise SandboxExecutionError("Sandbox session is closed.")
            if self._container is not None:
                return self._container
            docker = _import_docker()
            client = docker.from_env()
            self._pull_image_if_missing(docker, client)
            skeleton = Path(tempfile.mkdtemp(prefix="dagent-sandbox-skel-"))
            build_skeleton(skeleton)
            self._skeleton_dir = skeleton
            self._container = client.containers.run(
                self.config.image,
                ["sleep", "infinity"],
                detach=True,
                working_dir=str(self.workspace_root),
                volumes=self._volumes(skeleton),
                environment={
                    "PYTHONPATH": SKELETON_BIND,
                    "HOME": "/tmp",
                    **self.config.environment,
                },
                read_only=True,
                tmpfs=dict(self.config.tmpfs),
                network_disabled=not self.config.network,
                user=self._user,
                cap_drop=list(self.config.cap_drop),
                security_opt=list(self.config.security_opt),
                mem_limit=self.config.memory_limit,
                nano_cpus=self.config.nano_cpus,
                pids_limit=self.config.pids_limit,
                labels={**self.config.labels, "dagent.sandbox": "true"},
            )
            return self._container

    def _pull_image_if_missing(self, docker: Any, client: Any) -> None:
        try:
            client.images.get(self.config.image)
        except docker.errors.ImageNotFound:
            client.images.pull(self.config.image)

    def _volumes(self, skeleton: Path) -> dict[str, dict[str, str]]:
        volumes: dict[str, dict[str, str]] = {
            str(self.workspace_root): {"bind": str(self.workspace_root), "mode": "rw"},
            str(skeleton): {"bind": SKELETON_BIND, "mode": "ro"},
        }
        for skill_dir in self.skill_dirs:
            # identity mount; skip if nested under the workspace (already mounted)
            if skill_dir == self.workspace_root or str(skill_dir).startswith(f"{self.workspace_root}/"):
                continue
            volumes[str(skill_dir)] = {"bind": str(skill_dir), "mode": "ro"}
        return volumes

    def close(self) -> None:
        with self._lock:
            self._closed = True
            container = self._container
            self._container = None
            skeleton_dir = self._skeleton_dir
            self._skeleton_dir = None
        if container is not None:
            try:
                container.stop(timeout=5)
            except Exception as exc:  # noqa: BLE001 - cleanup must not raise
                _logger.warning("Failed to stop sandbox container: %s", exc)
            try:
                container.remove(force=True)
            except Exception as exc:  # noqa: BLE001 - cleanup must not raise
                _logger.warning("Failed to remove sandbox container: %s", exc)
        if skeleton_dir is not None:
            shutil.rmtree(skeleton_dir, ignore_errors=True)

    # -- execution ---------------------------------------------------------

    def _clamp_tool_timeout(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Cap a tool's own ``timeout_seconds`` to the configured hard budget.

        The model can pass an arbitrary ``timeout_seconds`` (e.g. to ``shell``);
        without clamping it can exceed the host watchdog, which would then hard
        kill the worker with a generic timeout instead of honoring the budget.
        """
        requested = arguments.get("timeout_seconds")
        if requested is None:
            return arguments
        try:
            capped = min(int(requested), self.config.tool_timeout_seconds)
        except (TypeError, ValueError):
            return arguments
        if capped == requested:
            return arguments
        return {**arguments, "timeout_seconds": capped}

    def run_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolOutput:
        container = self._ensure_started()
        arguments = self._clamp_tool_timeout(arguments)
        request = base64.b64encode(
            json.dumps(
                {"tool": tool_name, "args": arguments},
                ensure_ascii=False,
                default=str,  # resolved path args arrive as Path objects
            ).encode("utf-8")
        ).decode("ascii")
        watchdog = self.config.tool_timeout_seconds + _WATCHDOG_GRACE_SECONDS
        exit_code, (stdout, stderr) = container.exec_run(
            ["timeout", str(watchdog), "python", WORKER_IN_CONTAINER, request],
            user=self._user,
            demux=True,
        )
        if exit_code == 124:
            raise SandboxExecutionError(
                f"Sandbox tool '{tool_name}' exceeded the hard limit of {watchdog}s."
            )
        out = (stdout or b"").decode("utf-8", errors="replace").strip()
        if exit_code != 0 or not out:
            err = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise SandboxExecutionError(
                f"Sandbox worker failed (exit_code={exit_code}).\n{err or out}".strip()
            )
        try:
            result = json.loads(out.splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise SandboxExecutionError("Sandbox result is not valid JSON.") from exc
        if not result.get("ok"):
            raise SandboxToolExecutionError(
                str(result.get("error") or "Sandbox tool failed."),
                stop_reason=str(result.get("error_type") or "SandboxToolExecutionError"),
            )
        return ToolOutput(content=str(result.get("content") or ""), value=result.get("value"))


def sandbox_status(config: SandboxConfig | None = None) -> dict[str, Any]:
    """Report docker availability and configuration (not a live container)."""
    config = config or SandboxConfig()
    status: dict[str, Any] = {
        "backend": config.backend,
        "image": config.docker.image,
        "network": config.docker.network,
    }
    try:
        docker = _import_docker()
        # Bound the probe so a hung/unreachable DOCKER_HOST cannot stall the
        # status call for the SDK's default (~60s) timeout.
        client = docker.from_env(timeout=5)
        status["docker_available"] = bool(client.ping())
    except Exception as exc:  # noqa: BLE001 - status endpoint should not raise
        status["docker_available"] = False
        status["error"] = str(exc)
    return status

"""Sandbox execution schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


RunExecution = Literal["local", "sandbox"]
SandboxBackend = Literal["docker"]


class DockerSandboxConfig(BaseModel):
    """Docker-backed sandbox settings.

    The run workspace and visible skill directories are bind-mounted into the
    container at their original host paths (identity mount), so no path
    translation is needed between host and container.
    """

    image: str = "python:3.12-slim"
    network: bool = False
    memory_limit: str | int | None = "512m"
    nano_cpus: int | None = 1_000_000_000
    pids_limit: int | None = 256
    user: str | None = None
    cap_drop: list[str] = Field(default_factory=lambda: ["ALL"])
    security_opt: list[str] = Field(default_factory=lambda: ["no-new-privileges"])
    tmpfs: dict[str, str] = Field(default_factory=lambda: {"/tmp": "rw,nosuid,nodev,size=64m"})
    environment: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    tool_timeout_seconds: int = 60


class SandboxConfig(BaseModel):
    """Runtime sandbox configuration."""

    backend: SandboxBackend = "docker"
    docker: DockerSandboxConfig = Field(default_factory=DockerSandboxConfig)

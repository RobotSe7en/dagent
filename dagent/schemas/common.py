"""Shared low-level schema primitives."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


BoundaryMode = Literal["read_only", "write_limited", "full"]
RiskLevel = Literal["low", "medium", "high"]


class Boundary(BaseModel):
    mode: BoundaryMode = "read_only"
    allowed_paths: list[str] = Field(default_factory=list)
    allowed_commands: list[str] = Field(default_factory=list)

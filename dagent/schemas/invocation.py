"""Tool invocation schemas shared by tool and DAG execution."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from dagent.schemas.common import Boundary, RiskLevel


class ToolInvocation(BaseModel):
    invocation_id: str = Field(default_factory=lambda: f"inv_{uuid4().hex}")
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    boundary: Boundary = Field(default_factory=Boundary)
    risk: RiskLevel = "low"

"""Execution feedback and pause/resume schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from dagent.schemas.node import Boundary


class PermissionRequest(BaseModel):
    request_id: str
    dag_id: str
    node_id: str
    reason: str
    violation: str
    requested_boundary: Boundary
    status: Literal["pending", "approved", "denied"] = "pending"


class ExecutionFeedback(BaseModel):
    node_id: str
    kind: Literal["permission", "execution"]
    message: str
    payload: dict = Field(default_factory=dict)

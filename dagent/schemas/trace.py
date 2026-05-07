"""Trace schemas for future run recording."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


TraceEventType = Literal[
    "dag_started",
    "dag_completed",
    "dag_failed",
    "dag_paused",
    "dag_replanned",
    "dag_replan_failed",
    "review_requested",
    "node_started",
    "node_completed",
    "node_failed",
    "node_blocked_permission",
    "tool_called",
    "tool_completed",
    "tool_failed",
    "permission_requested",
    "permission_approved",
    "permission_denied",
]


class TraceEvent(BaseModel):
    event_id: str
    event_type: TraceEventType
    dag_id: str
    node_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TraceSpan(BaseModel):
    span_id: str
    dag_id: str
    node_id: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    events: list[TraceEvent] = Field(default_factory=list)


NodeExecutionStatus = Literal[
    "completed",
    "failed",
    "blocked_permission",
]


class NodeExecutionRecord(BaseModel):
    record_id: str
    task_id: str
    dag_id: str
    dag_version: int
    node_id: str
    node_title: str = ""
    node_goal: str = ""
    node_kind: str = "tool"
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    output: str = ""
    error: str | None = None
    status: NodeExecutionStatus
    stop_reason: str = ""
    steps: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


"""Trace schemas for future run recording."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from dagent.schemas.capability import CapabilityInvocation


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
    "capability_called",
    "capability_completed",
    "capability_failed",
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


CapabilityExecutionStatus = Literal[
    "completed",
    "failed",
]

CapabilityExecutionSource = Literal[
    "capability_loop",
    "dag_node",
]


class CapabilityExecutionRecord(BaseModel):
    record_id: str
    task_id: str
    invocation: CapabilityInvocation
    source: CapabilityExecutionSource
    output: str = ""
    error: str | None = None
    status: CapabilityExecutionStatus
    stop_reason: str = ""
    steps: int = 0
    dag_id: str | None = None
    dag_version: int | None = None
    node_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


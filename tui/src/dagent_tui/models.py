"""Small host-facing data models used by the TUI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


RunTarget = Literal["auto", "tool", "dag"]
ReviewLevel = Literal["fast", "careful"]


@dataclass(frozen=True)
class Project:
    id: str
    name: str

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "Project":
        return cls(id=str(value["id"]), name=str(value.get("name") or value["id"]))


@dataclass(frozen=True)
class Conversation:
    id: str
    title: str
    project_id: str | None = None
    kind: str = "chat"
    last_run_id: str | None = None

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "Conversation":
        project_id = value.get("project_id")
        last_run_id = value.get("last_run_id")
        return cls(
            id=str(value["id"]),
            title=str(value.get("title") or value["id"]),
            project_id=None if project_id is None else str(project_id),
            kind=str(value.get("kind") or "chat"),
            last_run_id=None if last_run_id is None else str(last_run_id),
        )


@dataclass(frozen=True)
class Navigation:
    standalone: tuple[Conversation, ...] = ()
    projects: tuple[tuple[Project, tuple[Conversation, ...]], ...] = ()


@dataclass(frozen=True)
class ConversationMessage:
    role: Literal["user", "assistant"]
    content: str
    status: str = "completed"
    run_id: str | None = None
    timeline: tuple[dict[str, Any], ...] = ()
    dag: dict[str, Any] | None = None
    trace: dict[str, Any] | None = None
    pending_review: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "ConversationMessage":
        timeline = value.get("timeline")
        return cls(
            role="assistant" if value.get("role") == "assistant" else "user",
            content=str(value.get("content") or ""),
            status=str(value.get("status") or "completed"),
            run_id=None if value.get("run_id") is None else str(value["run_id"]),
            timeline=tuple(item for item in timeline or () if isinstance(item, dict)),
            dag=value.get("dag") if isinstance(value.get("dag"), dict) else None,
            trace=value.get("trace") if isinstance(value.get("trace"), dict) else None,
            pending_review=(
                value.get("pending_review")
                if isinstance(value.get("pending_review"), dict)
                else None
            ),
        )


@dataclass(frozen=True)
class StreamEnvelope:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0
    run_id: str | None = None

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "StreamEnvelope":
        data = value.get("data")
        run_id = value.get("run_id")
        sequence = value.get("sequence", 0)
        return cls(
            type=str(value.get("type") or ""),
            data=data if isinstance(data, dict) else {},
            sequence=int(sequence) if isinstance(sequence, int) else 0,
            run_id=None if run_id is None else str(run_id),
        )


@dataclass(frozen=True)
class ReviewDecision:
    approved: bool
    feedback: str = ""

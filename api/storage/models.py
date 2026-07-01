from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


RunStatus = Literal["queued", "running", "awaiting_review", "completed", "failed"]
RunExecution = Literal["local", "sandbox", "worker"]
ReviewStatus = Literal["pending", "resolved"]
ConversationKind = Literal["chat", "dynamic_dag", "static_dag"]
OrchestrationKind = Literal["dynamic_dag", "static_dag"]


class Project(BaseModel):
    id: str
    org_id: str = "default"
    owner_user_id: str = "default"
    slug: str
    name: str
    description: str | None = None
    workspace_uri: str
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: int
    updated_at: int
    archived_at: int | None = None


class Conversation(BaseModel):
    id: str
    project_id: str | None = None
    org_id: str = "default"
    owner_user_id: str = "default"
    kind: ConversationKind = "chat"
    title: str
    status: str = "active"
    workspace_uri: str
    last_run_id: str | None = None
    created_at: int
    updated_at: int
    archived_at: int | None = None


class Run(BaseModel):
    id: str
    project_id: str | None = None
    conversation_id: str | None = None
    org_id: str = "default"
    user_id: str = "default"
    kind: str | None = None
    status: RunStatus
    execution: RunExecution = "local"
    workspace_uri: str
    saved_dag_id: str | None = None
    state_json: str | None = None
    output_text: str = ""
    error_json: str | None = None
    lease_owner: str | None = None
    lease_expires_at: int | None = None
    created_at: int
    started_at: int | None = None
    completed_at: int | None = None
    updated_at: int


class RunStream(BaseModel):
    id: str
    run_id: str
    project_id: str | None = None
    conversation_id: str | None = None
    org_id: str = "default"
    user_id: str = "default"
    kind: str
    status: RunStatus
    started_at: int
    completed_at: int | None = None
    error_json: str | None = None


class RunEvent(BaseModel):
    run_id: str
    event_id: int
    stream_id: str
    stream_seq: int
    event_type: str
    payload_json: str
    created_at: int


class Review(BaseModel):
    id: str
    run_id: str
    project_id: str | None = None
    org_id: str = "default"
    kind: str
    status: ReviewStatus
    decision_json: str | None = None
    created_at: int
    resolved_at: int | None = None


class SavedDag(BaseModel):
    id: str
    project_id: str | None = None
    org_id: str = "default"
    owner_user_id: str = "default"
    name: str
    description: str = ""
    spec_json: str
    layout_json: str = "{}"
    revision: int = 1
    created_at: int
    updated_at: int
    archived_at: int | None = None


class OrchestrationSession(BaseModel):
    id: str
    conversation_id: str
    project_id: str | None = None
    kind: OrchestrationKind
    saved_dag_id: str | None = None
    draft_dag_json: str | None = None
    ui_state_json: str = "{}"
    created_at: int
    updated_at: int

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dagent import RunState

from api.storage.models import Conversation, Project, Review, Run, RunEvent, RunExecution, RunStatus, RunStream


class ConversationBusyError(RuntimeError):
    """Raised when a conversation is already being driven by another stream."""


class StorageConflictError(RuntimeError):
    """Raised when a stored resource violates a uniqueness constraint."""


class ConversationLock(Protocol):
    @property
    def conversation_id(self) -> str: ...

    @property
    def owner(self) -> str: ...

    def release(self) -> None: ...


@dataclass(frozen=True)
class CreateProject:
    project_id: str
    slug: str
    name: str
    workspace_uri: str
    org_id: str = "default"
    owner_user_id: str = "default"
    description: str | None = None


class Store(Protocol):
    def close(self) -> None: ...

    def create_project(
        self,
        *,
        project_id: str,
        slug: str,
        name: str,
        workspace_uri: str,
        org_id: str = "default",
        owner_user_id: str = "default",
        description: str | None = None,
    ) -> Project: ...

    def list_projects(self, *, org_id: str = "default") -> list[Project]: ...

    def get_project(self, project_id: str, *, org_id: str | None = None) -> Project | None: ...

    def create_conversation(
        self,
        *,
        conversation_id: str,
        project_id: str,
        title: str,
        org_id: str = "default",
    ) -> Conversation: ...

    def list_conversations(self, project_id: str, *, org_id: str | None = None) -> list[Conversation]: ...

    def get_conversation(self, conversation_id: str, *, org_id: str | None = None) -> Conversation | None: ...

    def acquire_conversation_lock(self, conversation_id: str, *, owner: str) -> ConversationLock: ...

    def create_run(
        self,
        *,
        run_id: str,
        project_id: str,
        conversation_id: str | None,
        user_id: str,
        kind: str | None,
        status: RunStatus,
        workspace_uri: str,
        org_id: str = "default",
        execution: RunExecution = "local",
    ) -> Run: ...

    def get_run(self, run_id: str, *, org_id: str | None = None) -> Run | None: ...

    def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        started_at: int | None = None,
        completed_at: int | None = None,
    ) -> None: ...

    def create_run_stream(
        self,
        *,
        stream_id: str,
        run_id: str,
        project_id: str,
        conversation_id: str | None,
        user_id: str,
        kind: str,
        status: RunStatus,
        org_id: str = "default",
    ) -> RunStream: ...

    def append_run_event(
        self,
        *,
        run_id: str,
        stream_id: str,
        event_type: str,
        payload_json: str,
    ) -> RunEvent: ...

    def list_run_events(self, run_id: str, *, after_event_id: int = 0) -> list[RunEvent]: ...

    def save_run_state(self, run_id: str, state_json: str, output_text: str) -> None: ...

    def get_run_state(self, run_id: str) -> RunState | None: ...

    def save_run_error(self, run_id: str, error_json: str) -> None: ...

    def upsert_review(
        self,
        *,
        review_id: str,
        run_id: str,
        project_id: str,
        kind: str,
        org_id: str = "default",
    ) -> Review: ...

    def get_review(self, review_id: str, *, org_id: str | None = None) -> Review | None: ...

    def resolve_review(self, review_id: str, decision_json: str) -> None: ...

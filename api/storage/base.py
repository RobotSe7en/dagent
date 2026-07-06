from __future__ import annotations

from typing import Protocol

from dagent import RunState

from api.storage.models import (
    Conversation,
    ConversationKind,
    OrchestrationKind,
    OrchestrationSession,
    Project,
    Review,
    Run,
    RunEvent,
    RunExecution,
    RunStatus,
    RunStream,
    SavedDag,
)


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

    def update_project(
        self,
        project_id: str,
        *,
        slug: str,
        name: str,
        description: str | None,
        org_id: str = "default",
    ) -> Project: ...

    def delete_project(self, project_id: str, *, org_id: str | None = None) -> bool: ...

    def create_conversation(
        self,
        *,
        conversation_id: str,
        project_id: str | None,
        title: str,
        workspace_uri: str,
        org_id: str = "default",
        owner_user_id: str = "default",
        kind: ConversationKind = "chat",
    ) -> Conversation: ...

    def list_conversations(
        self,
        project_id: str | None = None,
        *,
        standalone: bool = False,
        org_id: str | None = None,
        kind: ConversationKind | None = None,
    ) -> list[Conversation]: ...

    def get_conversation(self, conversation_id: str, *, org_id: str | None = None) -> Conversation | None: ...

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str,
        org_id: str = "default",
    ) -> Conversation: ...

    def acquire_conversation_lock(
        self,
        conversation_id: str,
        *,
        owner: str,
        lease_seconds: int = 300,
    ) -> ConversationLock: ...

    def touch_conversation(self, conversation_id: str, *, updated_at: int | None = None) -> None: ...

    def create_run(
        self,
        *,
        run_id: str,
        project_id: str | None,
        conversation_id: str | None,
        user_id: str,
        kind: str | None,
        status: RunStatus,
        workspace_uri: str,
        org_id: str = "default",
        execution: RunExecution = "local",
        saved_dag_id: str | None = None,
    ) -> Run: ...

    def get_run(self, run_id: str, *, org_id: str | None = None) -> Run | None: ...

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        conversation_id: str | None = None,
        saved_dag_id: str | None = None,
        org_id: str | None = None,
    ) -> list[Run]: ...

    def delete_run(self, run_id: str, *, org_id: str | None = None) -> bool: ...

    def delete_conversation(self, conversation_id: str, *, org_id: str | None = None) -> bool: ...

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
        project_id: str | None,
        conversation_id: str | None,
        user_id: str,
        kind: str,
        status: RunStatus,
        org_id: str = "default",
    ) -> RunStream: ...

    def list_run_streams(self, run_id: str) -> list[RunStream]: ...

    def finish_run_stream(
        self,
        stream_id: str,
        status: RunStatus,
        *,
        error_json: str | None = None,
        completed_at: int | None = None,
    ) -> None: ...

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
        project_id: str | None,
        kind: str,
        org_id: str = "default",
    ) -> Review: ...

    def get_review(self, review_id: str, *, org_id: str | None = None) -> Review | None: ...

    def resolve_review(self, review_id: str, decision_json: str) -> None: ...

    def create_saved_dag(
        self,
        *,
        dag_id: str,
        project_id: str | None,
        name: str,
        description: str,
        spec_json: str,
        layout_json: str = "{}",
        org_id: str = "default",
        owner_user_id: str = "default",
    ) -> SavedDag: ...

    def get_saved_dag(self, dag_id: str, *, org_id: str | None = None) -> SavedDag | None: ...

    def list_saved_dags(
        self,
        project_id: str | None = None,
        *,
        org_id: str | None = None,
    ) -> list[SavedDag]: ...

    def update_saved_dag(
        self,
        dag_id: str,
        *,
        name: str,
        description: str,
        spec_json: str,
        layout_json: str,
        expected_revision: int | None = None,
        org_id: str = "default",
    ) -> SavedDag: ...

    def archive_saved_dag(self, dag_id: str, *, org_id: str | None = None) -> bool: ...

    def create_orchestration_session(
        self,
        *,
        session_id: str,
        conversation_id: str,
        project_id: str | None,
        kind: OrchestrationKind,
        saved_dag_id: str | None = None,
        draft_dag_json: str | None = None,
        ui_state_json: str = "{}",
    ) -> OrchestrationSession: ...

    def get_orchestration_session(self, session_id: str) -> OrchestrationSession | None: ...

    def get_orchestration_session_by_conversation(self, conversation_id: str) -> OrchestrationSession | None: ...

    def update_orchestration_session(
        self,
        session_id: str,
        *,
        saved_dag_id: str | None = None,
        draft_dag_json: str | None = None,
        ui_state_json: str | None = None,
        update_saved_dag_id: bool = False,
        update_draft_dag: bool = False,
        update_ui_state: bool = False,
    ) -> OrchestrationSession: ...

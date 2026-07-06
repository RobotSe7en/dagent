from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from dagent import RunState

from api.storage.base import ConversationBusyError, StorageConflictError
from api.storage.models import (
    Conversation,
    ConversationKind,
    ConversationMessage,
    ConversationMessageRole,
    ConversationMessageStatus,
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


class _SQLiteConversationLock:
    def __init__(self, store: SQLiteStore, conversation_id: str, owner: str, lease_seconds: int) -> None:
        self._store = store
        self._conversation_id = conversation_id
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._released = False
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None
        if lease_seconds > 0:
            interval = max(1.0, min(30.0, lease_seconds / 3))
            self._heartbeat = threading.Thread(target=self._renew_until_released, args=(interval,), daemon=True)
            self._heartbeat.start()

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def owner(self) -> str:
        return self._owner

    def release(self) -> None:
        if self._released:
            return
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=1)
        self._store._release_conversation_lock(self._conversation_id, self._owner)
        self._released = True

    def _renew_until_released(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self._store._renew_conversation_lock(self._conversation_id, self._owner, self._lease_seconds)
            except sqlite3.Error:
                return

    def __enter__(self) -> _SQLiteConversationLock:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._memory = str(path) == ":memory:"
        if not self._memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._configure()
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _configure(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA journal_mode=WAL")

    def _migrate(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self._lock:
            if self._needs_schema_rebuild():
                self._rebuild_database()
            self._conn.executescript(schema)
            self._conn.execute("DELETE FROM conversation_locks WHERE expires_at <= ?", (_now(),))
            self._conn.commit()

    def _needs_schema_rebuild(self) -> bool:
        tables = {
            row["name"]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not tables:
            return False
        required_tables = {
            "projects",
            "conversations",
            "conversation_locks",
            "runs",
            "run_streams",
            "run_events",
            "reviews",
            "saved_dags",
            "orchestration_sessions",
        }
        if not required_tables.issubset(tables):
            return True
        return (
            "kind" not in self._table_columns("conversations")
            or "saved_dag_id" not in self._table_columns("runs")
        )

    def _table_columns(self, table: str) -> set[str]:
        return {
            row["name"]
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _rebuild_database(self) -> None:
        self._conn.close()
        if not self._memory:
            self.path.unlink(missing_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure()

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
    ) -> Project:
        now = _now()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO projects(
                        id, org_id, owner_user_id, slug, name, description,
                        workspace_uri, settings_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
                    """,
                    (project_id, org_id, owner_user_id, slug, name, description, workspace_uri, now, now),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise StorageConflictError(str(exc)) from exc
            row = self._required_row("SELECT * FROM projects WHERE id = ?", (project_id,))
        return _project_from_row(row)

    def list_projects(self, *, org_id: str = "default") -> list[Project]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM projects WHERE org_id = ? AND archived_at IS NULL ORDER BY updated_at DESC",
                (org_id,),
            ).fetchall()
        return [_project_from_row(row) for row in rows]

    def get_project(self, project_id: str, *, org_id: str | None = None) -> Project | None:
        query = "SELECT * FROM projects WHERE id = ?"
        params: tuple[object, ...] = (project_id,)
        if org_id is not None:
            query += " AND org_id = ?"
            params = (project_id, org_id)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return None if row is None else _project_from_row(row)

    def update_project(
        self,
        project_id: str,
        *,
        slug: str,
        name: str,
        description: str | None,
        org_id: str = "default",
    ) -> Project:
        now = _now()
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    UPDATE projects
                    SET slug = ?, name = ?, description = ?, updated_at = ?
                    WHERE id = ? AND org_id = ?
                    """,
                    (slug, name, description, now, project_id, org_id),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise StorageConflictError(str(exc)) from exc
            if cursor.rowcount == 0:
                raise KeyError(f"Project '{project_id}' not found.")
            row = self._required_row("SELECT * FROM projects WHERE id = ?", (project_id,))
        return _project_from_row(row)

    def delete_project(self, project_id: str, *, org_id: str | None = None) -> bool:
        query = "DELETE FROM projects WHERE id = ?"
        params: tuple[object, ...] = (project_id,)
        if org_id is not None:
            query += " AND org_id = ?"
            params = (project_id, org_id)
        with self._lock:
            cursor = self._conn.execute(query, params)
            self._conn.commit()
        return cursor.rowcount > 0

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
    ) -> Conversation:
        now = _now()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO conversations(
                        id, project_id, org_id, owner_user_id, kind,
                        title, status, workspace_uri, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (conversation_id, project_id, org_id, owner_user_id, kind, title, workspace_uri, now, now),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise StorageConflictError(str(exc)) from exc
            row = self._required_row("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        return _conversation_from_row(row)

    def list_conversations(
        self,
        project_id: str | None = None,
        *,
        standalone: bool = False,
        org_id: str | None = None,
        kind: ConversationKind | None = None,
    ) -> list[Conversation]:
        conditions = ["archived_at IS NULL"]
        params: list[object] = []
        if standalone:
            conditions.append("project_id IS NULL")
        elif project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        if org_id is not None:
            conditions.append("org_id = ?")
            params.append(org_id)
        if kind is not None:
            conditions.append("kind = ?")
            params.append(kind)
        query = "SELECT * FROM conversations WHERE " + " AND ".join(conditions)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [_conversation_from_row(row) for row in rows]

    def get_conversation(self, conversation_id: str, *, org_id: str | None = None) -> Conversation | None:
        query = "SELECT * FROM conversations WHERE id = ?"
        params: tuple[object, ...] = (conversation_id,)
        if org_id is not None:
            query += " AND org_id = ?"
            params = (conversation_id, org_id)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return None if row is None else _conversation_from_row(row)

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str,
        org_id: str = "default",
    ) -> Conversation:
        now = _now()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE conversations
                SET title = ?, updated_at = ?
                WHERE id = ? AND org_id = ? AND archived_at IS NULL
                """,
                (title, now, conversation_id, org_id),
            )
            if cursor.rowcount == 0:
                self._conn.rollback()
                raise KeyError(f"Conversation '{conversation_id}' not found.")
            self._conn.commit()
            row = self._required_row(
                "SELECT * FROM conversations WHERE id = ? AND org_id = ?",
                (conversation_id, org_id),
            )
        return _conversation_from_row(row)

    def acquire_conversation_lock(
        self,
        conversation_id: str,
        *,
        owner: str,
        lease_seconds: int = 300,
    ) -> _SQLiteConversationLock:
        now = _now()
        lease_seconds = max(0, int(lease_seconds))
        expires_at = now + lease_seconds
        with self._lock:
            if self._conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone() is None:
                raise KeyError(f"Conversation '{conversation_id}' not found.")
            self._conn.execute(
                "DELETE FROM conversation_locks WHERE conversation_id = ? AND expires_at <= ?",
                (conversation_id, now),
            )
            try:
                self._conn.execute(
                    """
                    INSERT INTO conversation_locks(conversation_id, owner, acquired_at, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (conversation_id, owner, now, expires_at),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                active = self._conn.execute(
                    "SELECT owner, expires_at FROM conversation_locks WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if active is None:
                    raise
                if active["expires_at"] <= now:
                    self._conn.execute("DELETE FROM conversation_locks WHERE conversation_id = ?", (conversation_id,))
                    self._conn.execute(
                        """
                        INSERT INTO conversation_locks(conversation_id, owner, acquired_at, expires_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (conversation_id, owner, now, expires_at),
                    )
                    self._conn.commit()
                    return _SQLiteConversationLock(self, conversation_id, owner, lease_seconds)
                if active["owner"] != owner:
                    raise ConversationBusyError(f"Conversation '{conversation_id}' is already active.") from exc
                self._conn.execute(
                    """
                    UPDATE conversation_locks
                    SET acquired_at = ?, expires_at = ?
                    WHERE conversation_id = ? AND owner = ?
                    """,
                    (now, expires_at, conversation_id, owner),
                )
                self._conn.commit()
        return _SQLiteConversationLock(self, conversation_id, owner, lease_seconds)

    def touch_conversation(self, conversation_id: str, *, updated_at: int | None = None) -> None:
        now = _now() if updated_at is None else updated_at
        with self._lock:
            self._touch_conversation_locked(conversation_id, now)
            self._conn.commit()

    def _release_conversation_lock(self, conversation_id: str, owner: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM conversation_locks WHERE conversation_id = ? AND owner = ?",
                (conversation_id, owner),
            )
            self._conn.commit()

    def _renew_conversation_lock(self, conversation_id: str, owner: str, lease_seconds: int) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE conversation_locks
                SET expires_at = ?
                WHERE conversation_id = ? AND owner = ?
                """,
                (now + lease_seconds, conversation_id, owner),
            )
            self._conn.commit()

    def _touch_conversation_locked(self, conversation_id: str, updated_at: int) -> None:
        self._conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (updated_at, conversation_id),
        )

    def _touch_run_conversation_locked(self, run_id: str, updated_at: int) -> None:
        self._conn.execute(
            """
            UPDATE conversations
            SET updated_at = ?
            WHERE id = (SELECT conversation_id FROM runs WHERE id = ?)
            """,
            (updated_at, run_id),
        )

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
    ) -> Run:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs(
                    id, project_id, conversation_id, org_id, user_id, kind, status,
                    execution, workspace_uri, saved_dag_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    project_id,
                    conversation_id,
                    org_id,
                    user_id,
                    kind,
                    status,
                    execution,
                    workspace_uri,
                    saved_dag_id,
                    now,
                    now,
                ),
            )
            if conversation_id is not None:
                self._conn.execute(
                    "UPDATE conversations SET last_run_id = ?, updated_at = ? WHERE id = ?",
                    (run_id, now, conversation_id),
                )
            self._conn.commit()
            row = self._required_row("SELECT * FROM runs WHERE id = ?", (run_id,))
        return _run_from_row(row)

    def get_run(self, run_id: str, *, org_id: str | None = None) -> Run | None:
        query = "SELECT * FROM runs WHERE id = ?"
        params: tuple[object, ...] = (run_id,)
        if org_id is not None:
            query += " AND org_id = ?"
            params = (run_id, org_id)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return None if row is None else _run_from_row(row)

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        conversation_id: str | None = None,
        saved_dag_id: str | None = None,
        org_id: str | None = None,
    ) -> list[Run]:
        conditions: list[str] = []
        params: list[object] = []
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        if conversation_id is not None:
            conditions.append("conversation_id = ?")
            params.append(conversation_id)
        if saved_dag_id is not None:
            conditions.append("saved_dag_id = ?")
            params.append(saved_dag_id)
        if org_id is not None:
            conditions.append("org_id = ?")
            params.append(org_id)
        query = "SELECT * FROM runs"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [_run_from_row(row) for row in rows]

    def delete_run(self, run_id: str, *, org_id: str | None = None) -> bool:
        select_query = "SELECT conversation_id FROM runs WHERE id = ?"
        query = "DELETE FROM runs WHERE id = ?"
        params: tuple[object, ...] = (run_id,)
        if org_id is not None:
            select_query += " AND org_id = ?"
            query += " AND org_id = ?"
            params = (run_id, org_id)
        with self._lock:
            row = self._conn.execute(select_query, params).fetchone()
            if row is None:
                return False
            cursor = self._conn.execute(query, params)
            conversation_id = row["conversation_id"]
            if conversation_id is not None:
                replacement = self._conn.execute(
                    """
                    SELECT id FROM runs
                    WHERE conversation_id = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (conversation_id,),
                ).fetchone()
                self._conn.execute(
                    """
                    UPDATE conversations
                    SET last_run_id = ?, updated_at = ?
                    WHERE id = ? AND last_run_id = ?
                    """,
                    (
                        None if replacement is None else replacement["id"],
                        _now(),
                        conversation_id,
                        run_id,
                    ),
                )
            self._conn.commit()
        return cursor.rowcount > 0

    def delete_conversation(self, conversation_id: str, *, org_id: str | None = None) -> bool:
        query = "DELETE FROM conversations WHERE id = ?"
        params: tuple[object, ...] = (conversation_id,)
        if org_id is not None:
            query += " AND org_id = ?"
            params = (conversation_id, org_id)
        with self._lock:
            cursor = self._conn.execute(query, params)
            self._conn.commit()
        return cursor.rowcount > 0

    def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        started_at: int | None = None,
        completed_at: int | None = None,
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE runs
                SET status = ?,
                    started_at = COALESCE(?, started_at),
                    completed_at = COALESCE(?, completed_at),
                    updated_at = ?
                WHERE id = ?
                """,
                (status, started_at, completed_at, now, run_id),
            )
            self._touch_run_conversation_locked(run_id, now)
            self._conn.commit()

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
    ) -> RunStream:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO run_streams(
                    id, run_id, project_id, conversation_id, org_id, user_id,
                    kind, status, started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (stream_id, run_id, project_id, conversation_id, org_id, user_id, kind, status, now),
            )
            if conversation_id is not None:
                self._touch_conversation_locked(conversation_id, now)
            self._conn.commit()
            row = self._required_row("SELECT * FROM run_streams WHERE id = ?", (stream_id,))
        return _run_stream_from_row(row)

    def list_run_streams(self, run_id: str) -> list[RunStream]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM run_streams WHERE run_id = ? ORDER BY started_at ASC, id ASC",
                (run_id,),
            ).fetchall()
        return [_run_stream_from_row(row) for row in rows]

    def finish_run_stream(
        self,
        stream_id: str,
        status: RunStatus,
        *,
        error_json: str | None = None,
        completed_at: int | None = None,
    ) -> None:
        completed = completed_at if completed_at is not None else _now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE run_streams
                SET status = ?, completed_at = ?, error_json = ?
                WHERE id = ?
                """,
                (status, completed, error_json, stream_id),
            )
            self._conn.commit()

    def append_run_event(
        self,
        *,
        run_id: str,
        stream_id: str,
        event_type: str,
        payload_json: str,
    ) -> RunEvent:
        created_at = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                event_id = int(
                    self._conn.execute(
                        "SELECT COALESCE(MAX(event_id), 0) + 1 FROM run_events WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()[0]
                )
                stream_seq = int(
                    self._conn.execute(
                        """
                        SELECT COALESCE(MAX(stream_seq), -1) + 1
                        FROM run_events
                        WHERE run_id = ? AND stream_id = ?
                        """,
                        (run_id, stream_id),
                    ).fetchone()[0]
                )
                self._conn.execute(
                    """
                    INSERT INTO run_events(
                        run_id, event_id, stream_id, stream_seq,
                        event_type, payload_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, event_id, stream_id, stream_seq, event_type, payload_json, created_at),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            row = self._required_row(
                "SELECT * FROM run_events WHERE run_id = ? AND event_id = ?",
                (run_id, event_id),
            )
        return _run_event_from_row(row)

    def list_run_events(self, run_id: str, *, after_event_id: int = 0) -> list[RunEvent]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM run_events
                WHERE run_id = ? AND event_id > ?
                ORDER BY event_id ASC
                """,
                (run_id, after_event_id),
            ).fetchall()
        return [_run_event_from_row(row) for row in rows]

    def append_conversation_message(
        self,
        *,
        message_id: str,
        conversation_id: str,
        project_id: str | None,
        role: ConversationMessageRole,
        content: str = "",
        run_id: str | None = None,
        status: ConversationMessageStatus = "created",
        timeline_json: str = "[]",
        dag_json: str | None = None,
        trace_json: str | None = None,
        pending_review_json: str | None = None,
        org_id: str = "default",
    ) -> ConversationMessage:
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                turn_index = int(
                    self._conn.execute(
                        "SELECT COALESCE(MAX(turn_index), -1) + 1 FROM conversation_messages WHERE conversation_id = ?",
                        (conversation_id,),
                    ).fetchone()[0]
                )
                self._conn.execute(
                    """
                    INSERT INTO conversation_messages(
                        id, conversation_id, project_id, org_id, role, run_id,
                        turn_index, status, content, timeline_json, dag_json,
                        trace_json, pending_review_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        conversation_id,
                        project_id,
                        org_id,
                        role,
                        run_id,
                        turn_index,
                        status,
                        content,
                        timeline_json,
                        dag_json,
                        trace_json,
                        pending_review_json,
                        now,
                        now,
                    ),
                )
                self._touch_conversation_locked(conversation_id, now)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            row = self._required_row("SELECT * FROM conversation_messages WHERE id = ?", (message_id,))
        return _conversation_message_from_row(row)

    def update_conversation_message(
        self,
        message_id: str,
        *,
        content: str,
        status: ConversationMessageStatus,
        timeline_json: str,
        run_id: str | None = None,
        dag_json: str | None = None,
        trace_json: str | None = None,
        pending_review_json: str | None = None,
        org_id: str = "default",
    ) -> ConversationMessage:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE conversation_messages
                SET content = ?, status = ?, timeline_json = ?, run_id = ?,
                    dag_json = ?, trace_json = ?, pending_review_json = ?, updated_at = ?
                WHERE id = ? AND org_id = ?
                """,
                (
                    content,
                    status,
                    timeline_json,
                    run_id,
                    dag_json,
                    trace_json,
                    pending_review_json,
                    now,
                    message_id,
                    org_id,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM conversation_messages WHERE id = ? AND org_id = ?",
                (message_id, org_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Conversation message '{message_id}' not found.")
            self._touch_conversation_locked(row["conversation_id"], now)
            self._conn.commit()
        return _conversation_message_from_row(row)

    def list_conversation_messages(
        self,
        conversation_id: str,
        *,
        org_id: str | None = None,
    ) -> list[ConversationMessage]:
        query = "SELECT * FROM conversation_messages WHERE conversation_id = ?"
        params: list[object] = [conversation_id]
        if org_id is not None:
            query += " AND org_id = ?"
            params.append(org_id)
        query += " ORDER BY turn_index ASC, created_at ASC"
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [_conversation_message_from_row(row) for row in rows]

    def get_last_assistant_message_for_run(
        self,
        conversation_id: str,
        run_id: str,
        *,
        org_id: str | None = None,
    ) -> ConversationMessage | None:
        query = """
            SELECT * FROM conversation_messages
            WHERE conversation_id = ? AND run_id = ? AND role = 'assistant'
        """
        params: list[object] = [conversation_id, run_id]
        if org_id is not None:
            query += " AND org_id = ?"
            params.append(org_id)
        query += " ORDER BY turn_index DESC, created_at DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, tuple(params)).fetchone()
        return None if row is None else _conversation_message_from_row(row)

    def save_run_state(self, run_id: str, state_json: str, output_text: str) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE runs
                SET state_json = ?, output_text = ?, updated_at = ?
                WHERE id = ?
                """,
                (state_json, output_text, now, run_id),
            )
            self._touch_run_conversation_locked(run_id, now)
            self._conn.commit()

    def get_run_state(self, run_id: str) -> RunState | None:
        with self._lock:
            row = self._conn.execute("SELECT state_json FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None or row["state_json"] is None:
            return None
        return RunState.model_validate_json(row["state_json"])

    def save_run_error(self, run_id: str, error_json: str) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET error_json = ?, status = 'failed', updated_at = ? WHERE id = ?",
                (error_json, now, run_id),
            )
            self._touch_run_conversation_locked(run_id, now)
            self._conn.commit()

    def upsert_review(
        self,
        *,
        review_id: str,
        run_id: str,
        project_id: str | None,
        kind: str,
        org_id: str = "default",
    ) -> Review:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO reviews(id, run_id, project_id, org_id, kind, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(id) DO UPDATE SET
                    run_id = excluded.run_id,
                    project_id = excluded.project_id,
                    org_id = excluded.org_id,
                    kind = excluded.kind,
                    status = 'pending',
                    resolved_at = NULL
                """,
                (review_id, run_id, project_id, org_id, kind, now),
            )
            self._conn.commit()
            row = self._required_row("SELECT * FROM reviews WHERE id = ?", (review_id,))
        return _review_from_row(row)

    def get_review(self, review_id: str, *, org_id: str | None = None) -> Review | None:
        query = "SELECT * FROM reviews WHERE id = ?"
        params: tuple[object, ...] = (review_id,)
        if org_id is not None:
            query += " AND org_id = ?"
            params = (review_id, org_id)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return None if row is None else _review_from_row(row)

    def resolve_review(self, review_id: str, decision_json: str) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE reviews
                SET status = 'resolved', decision_json = ?, resolved_at = ?
                WHERE id = ?
                """,
                (decision_json, now, review_id),
            )
            self._conn.commit()

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
    ) -> SavedDag:
        now = _now()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO saved_dags(
                        id, project_id, org_id, owner_user_id, name, description,
                        spec_json, layout_json, revision, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        dag_id,
                        project_id,
                        org_id,
                        owner_user_id,
                        name,
                        description,
                        spec_json,
                        layout_json,
                        now,
                        now,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise StorageConflictError(str(exc)) from exc
            row = self._required_row("SELECT * FROM saved_dags WHERE id = ?", (dag_id,))
        return _saved_dag_from_row(row)

    def get_saved_dag(self, dag_id: str, *, org_id: str | None = None) -> SavedDag | None:
        query = "SELECT * FROM saved_dags WHERE id = ? AND archived_at IS NULL"
        params: tuple[object, ...] = (dag_id,)
        if org_id is not None:
            query += " AND org_id = ?"
            params = (dag_id, org_id)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return None if row is None else _saved_dag_from_row(row)

    def list_saved_dags(
        self,
        project_id: str | None = None,
        *,
        org_id: str | None = None,
    ) -> list[SavedDag]:
        conditions = ["archived_at IS NULL"]
        params: list[object] = []
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        if org_id is not None:
            conditions.append("org_id = ?")
            params.append(org_id)
        query = "SELECT * FROM saved_dags WHERE " + " AND ".join(conditions)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [_saved_dag_from_row(row) for row in rows]

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
    ) -> SavedDag:
        now = _now()
        query = """
            UPDATE saved_dags
            SET name = ?,
                description = ?,
                spec_json = ?,
                layout_json = ?,
                revision = revision + 1,
                updated_at = ?
            WHERE id = ? AND org_id = ? AND archived_at IS NULL
        """
        params: list[object] = [name, description, spec_json, layout_json, now, dag_id, org_id]
        if expected_revision is not None:
            query += " AND revision = ?"
            params.append(expected_revision)
        with self._lock:
            cursor = self._conn.execute(query, tuple(params))
            if cursor.rowcount == 0:
                existing = self._conn.execute(
                    "SELECT 1 FROM saved_dags WHERE id = ? AND org_id = ? AND archived_at IS NULL",
                    (dag_id, org_id),
                ).fetchone()
                self._conn.rollback()
                if existing is None:
                    raise KeyError(f"Saved DAG '{dag_id}' not found.")
                raise StorageConflictError(f"Saved DAG '{dag_id}' revision conflict.")
            self._conn.commit()
            row = self._required_row("SELECT * FROM saved_dags WHERE id = ?", (dag_id,))
        return _saved_dag_from_row(row)

    def archive_saved_dag(self, dag_id: str, *, org_id: str | None = None) -> bool:
        now = _now()
        query = "UPDATE saved_dags SET archived_at = ?, updated_at = ? WHERE id = ? AND archived_at IS NULL"
        params: tuple[object, ...] = (now, now, dag_id)
        if org_id is not None:
            query += " AND org_id = ?"
            params = (now, now, dag_id, org_id)
        with self._lock:
            cursor = self._conn.execute(query, params)
            if cursor.rowcount > 0:
                self._conn.execute(
                    """
                    UPDATE orchestration_sessions
                    SET saved_dag_id = NULL,
                        updated_at = ?
                    WHERE saved_dag_id = ?
                    """,
                    (now, dag_id),
                )
            self._conn.commit()
        return cursor.rowcount > 0

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
    ) -> OrchestrationSession:
        now = _now()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO orchestration_sessions(
                        id, conversation_id, project_id, kind, saved_dag_id,
                        draft_dag_json, ui_state_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        conversation_id,
                        project_id,
                        kind,
                        saved_dag_id,
                        draft_dag_json,
                        ui_state_json,
                        now,
                        now,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise StorageConflictError(str(exc)) from exc
            row = self._required_row("SELECT * FROM orchestration_sessions WHERE id = ?", (session_id,))
        return _orchestration_session_from_row(row)

    def get_orchestration_session(self, session_id: str) -> OrchestrationSession | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM orchestration_sessions WHERE id = ?", (session_id,)).fetchone()
        return None if row is None else _orchestration_session_from_row(row)

    def get_orchestration_session_by_conversation(self, conversation_id: str) -> OrchestrationSession | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orchestration_sessions WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return None if row is None else _orchestration_session_from_row(row)

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
    ) -> OrchestrationSession:
        now = _now()
        assignments = ["updated_at = ?"]
        params: list[object] = [now]
        if update_saved_dag_id:
            assignments.append("saved_dag_id = ?")
            params.append(saved_dag_id)
        if update_draft_dag:
            assignments.append("draft_dag_json = ?")
            params.append(draft_dag_json)
        if update_ui_state:
            assignments.append("ui_state_json = ?")
            params.append(ui_state_json)
        params.append(session_id)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE orchestration_sessions SET {', '.join(assignments)} WHERE id = ?",
                tuple(params),
            )
            if cursor.rowcount == 0:
                self._conn.rollback()
                raise KeyError(f"Orchestration session '{session_id}' not found.")
            self._conn.commit()
            row = self._required_row("SELECT * FROM orchestration_sessions WHERE id = ?", (session_id,))
        return _orchestration_session_from_row(row)

    def _required_row(self, query: str, params: tuple[object, ...]) -> sqlite3.Row:
        row = self._conn.execute(query, params).fetchone()
        if row is None:
            raise RuntimeError(f"Expected row for query: {query}")
        return row


def _now() -> int:
    return int(time.time())


def _project_from_row(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        org_id=row["org_id"],
        owner_user_id=row["owner_user_id"],
        slug=row["slug"],
        name=row["name"],
        description=row["description"],
        workspace_uri=row["workspace_uri"],
        settings=json.loads(row["settings_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


def _conversation_from_row(row: sqlite3.Row) -> Conversation:
    return Conversation(
        id=row["id"],
        project_id=row["project_id"],
        org_id=row["org_id"],
        owner_user_id=row["owner_user_id"],
        kind=row["kind"],
        title=row["title"],
        status=row["status"],
        workspace_uri=row["workspace_uri"],
        last_run_id=row["last_run_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


def _run_from_row(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        project_id=row["project_id"],
        conversation_id=row["conversation_id"],
        org_id=row["org_id"],
        user_id=row["user_id"],
        kind=row["kind"],
        status=row["status"],
        execution=row["execution"],
        workspace_uri=row["workspace_uri"],
        saved_dag_id=row["saved_dag_id"],
        state_json=row["state_json"],
        output_text=row["output_text"],
        error_json=row["error_json"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        updated_at=row["updated_at"],
    )


def _run_stream_from_row(row: sqlite3.Row) -> RunStream:
    return RunStream(
        id=row["id"],
        run_id=row["run_id"],
        project_id=row["project_id"],
        conversation_id=row["conversation_id"],
        org_id=row["org_id"],
        user_id=row["user_id"],
        kind=row["kind"],
        status=row["status"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error_json=row["error_json"],
    )


def _run_event_from_row(row: sqlite3.Row) -> RunEvent:
    return RunEvent(
        run_id=row["run_id"],
        event_id=row["event_id"],
        stream_id=row["stream_id"],
        stream_seq=row["stream_seq"],
        event_type=row["event_type"],
        payload_json=row["payload_json"],
        created_at=row["created_at"],
    )


def _conversation_message_from_row(row: sqlite3.Row) -> ConversationMessage:
    return ConversationMessage(
        id=row["id"],
        conversation_id=row["conversation_id"],
        project_id=row["project_id"],
        org_id=row["org_id"],
        role=row["role"],
        run_id=row["run_id"],
        turn_index=row["turn_index"],
        status=row["status"],
        content=row["content"],
        timeline_json=row["timeline_json"],
        dag_json=row["dag_json"],
        trace_json=row["trace_json"],
        pending_review_json=row["pending_review_json"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _review_from_row(row: sqlite3.Row) -> Review:
    return Review(
        id=row["id"],
        run_id=row["run_id"],
        project_id=row["project_id"],
        org_id=row["org_id"],
        kind=row["kind"],
        status=row["status"],
        decision_json=row["decision_json"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


def _saved_dag_from_row(row: sqlite3.Row) -> SavedDag:
    return SavedDag(
        id=row["id"],
        project_id=row["project_id"],
        org_id=row["org_id"],
        owner_user_id=row["owner_user_id"],
        name=row["name"],
        description=row["description"],
        spec_json=row["spec_json"],
        layout_json=row["layout_json"],
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


def _orchestration_session_from_row(row: sqlite3.Row) -> OrchestrationSession:
    return OrchestrationSession(
        id=row["id"],
        conversation_id=row["conversation_id"],
        project_id=row["project_id"],
        kind=row["kind"],
        saved_dag_id=row["saved_dag_id"],
        draft_dag_json=row["draft_dag_json"],
        ui_state_json=row["ui_state_json"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

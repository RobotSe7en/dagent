from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from dagent import RunState

from api.storage.base import ConversationBusyError, StorageConflictError
from api.storage.models import Conversation, Project, Review, Run, RunEvent, RunExecution, RunStatus, RunStream


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
        if str(path) != ":memory:":
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
            self._conn.executescript(schema)
            self._ensure_column("conversations", "owner_user_id", "TEXT NOT NULL DEFAULT 'default'")
            self._ensure_column("conversation_locks", "expires_at", "INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("DELETE FROM conversation_locks WHERE expires_at <= ?", (_now(),))
            self._conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
    ) -> Conversation:
        now = _now()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO conversations(
                        id, project_id, org_id, owner_user_id, title, status, workspace_uri, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (conversation_id, project_id, org_id, owner_user_id, title, workspace_uri, now, now),
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
    ) -> list[Conversation]:
        conditions = ["archived_at IS NULL"]
        params: list[object] = []
        if standalone:
            conditions.append("project_id IS NULL")
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        if org_id is not None:
            conditions.append("org_id = ?")
            params.append(org_id)
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
    ) -> Run:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs(
                    id, project_id, conversation_id, org_id, user_id, kind, status,
                    execution, workspace_uri, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        query = "DELETE FROM runs WHERE id = ?"
        params: tuple[object, ...] = (run_id,)
        if org_id is not None:
            query += " AND org_id = ?"
            params = (run_id, org_id)
        with self._lock:
            cursor = self._conn.execute(query, params)
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

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from dagent import RunState

from api.storage.base import ConversationBusyError
from api.storage.models import Conversation, Project, Review, Run, RunEvent, RunExecution, RunStatus, RunStream


class _SQLiteConversationLock:
    def __init__(self, store: SQLiteStore, conversation_id: str, owner: str) -> None:
        self._store = store
        self._conversation_id = conversation_id
        self._owner = owner
        self._released = False

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def owner(self) -> str:
        return self._owner

    def release(self) -> None:
        if self._released:
            return
        self._store._release_conversation_lock(self._conversation_id, self._owner)
        self._released = True

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
        self._conversation_locks: dict[str, str] = {}
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
            self._conn.commit()

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

    def create_conversation(
        self,
        *,
        conversation_id: str,
        project_id: str,
        title: str,
        org_id: str = "default",
    ) -> Conversation:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversations(
                    id, project_id, org_id, title, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (conversation_id, project_id, org_id, title, now, now),
            )
            self._conn.commit()
            row = self._required_row("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        return _conversation_from_row(row)

    def list_conversations(self, project_id: str, *, org_id: str | None = None) -> list[Conversation]:
        query = "SELECT * FROM conversations WHERE project_id = ? AND archived_at IS NULL"
        params: tuple[object, ...] = (project_id,)
        if org_id is not None:
            query += " AND org_id = ?"
            params = (project_id, org_id)
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
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

    def acquire_conversation_lock(self, conversation_id: str, *, owner: str) -> _SQLiteConversationLock:
        with self._lock:
            if self._conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone() is None:
                raise KeyError(f"Conversation '{conversation_id}' not found.")
            active_owner = self._conversation_locks.get(conversation_id)
            if active_owner is not None and active_owner != owner:
                raise ConversationBusyError(f"Conversation '{conversation_id}' is already active.")
            self._conversation_locks[conversation_id] = owner
        return _SQLiteConversationLock(self, conversation_id, owner)

    def _release_conversation_lock(self, conversation_id: str, owner: str) -> None:
        with self._lock:
            if self._conversation_locks.get(conversation_id) == owner:
                del self._conversation_locks[conversation_id]

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
            self._conn.commit()

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
            self._conn.commit()
            row = self._required_row("SELECT * FROM run_streams WHERE id = ?", (stream_id,))
        return _run_stream_from_row(row)

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
            self._conn.commit()

    def upsert_review(
        self,
        *,
        review_id: str,
        run_id: str,
        project_id: str,
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
        title=row["title"],
        status=row["status"],
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

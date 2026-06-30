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
            self._ensure_v2_schema()
            self._conn.executescript(schema)
            self._conn.commit()

    def _ensure_v2_schema(self) -> None:
        table_checks = (
            ("conversations", ("workspace_uri",), ("project_id",)),
            ("runs", (), ("project_id",)),
            ("run_streams", (), ("project_id",)),
            ("reviews", (), ("project_id",)),
        )
        if not any(
            self._table_needs_rebuild(table, required_columns=columns, nullable_columns=nullable_columns)
            for table, columns, nullable_columns in table_checks
        ):
            return
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            if self._table_needs_rebuild(
                "conversations",
                required_columns=("workspace_uri",),
                nullable_columns=("project_id",),
            ):
                self._rebuild_conversations_table()
            if self._table_needs_rebuild("runs", required_columns=(), nullable_columns=("project_id",)):
                self._rebuild_runs_table()
            if self._table_needs_rebuild("run_streams", required_columns=(), nullable_columns=("project_id",)):
                self._rebuild_run_streams_table()
            if self._table_needs_rebuild("reviews", required_columns=(), nullable_columns=("project_id",)):
                self._rebuild_reviews_table()
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _table_needs_rebuild(
        self,
        table: str,
        *,
        required_columns: tuple[str, ...],
        nullable_columns: tuple[str, ...],
    ) -> bool:
        columns = {row["name"]: row for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if any(column not in columns for column in required_columns):
            return True
        return any(columns[column]["notnull"] for column in nullable_columns if column in columns)

    def _rebuild_conversations_table(self) -> None:
        rows = [dict(row) for row in self._conn.execute("SELECT * FROM conversations").fetchall()]
        self._conn.execute("DROP TABLE conversations")
        self._conn.execute(
            """
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
                org_id TEXT NOT NULL DEFAULT 'default',
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                workspace_uri TEXT NOT NULL,
                last_run_id TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                archived_at INTEGER
            )
            """
        )
        for row in rows:
            workspace_uri = row.get("workspace_uri") or self._legacy_conversation_workspace_uri(
                row["id"],
                row.get("project_id"),
            )
            self._conn.execute(
                """
                INSERT INTO conversations(
                    id, project_id, org_id, title, status, workspace_uri,
                    last_run_id, created_at, updated_at, archived_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row.get("project_id"),
                    row.get("org_id", "default"),
                    row["title"],
                    row.get("status", "active"),
                    workspace_uri,
                    row.get("last_run_id"),
                    row["created_at"],
                    row["updated_at"],
                    row.get("archived_at"),
                ),
            )

    def _legacy_conversation_workspace_uri(self, conversation_id: str, project_id: str | None) -> str:
        if project_id is not None:
            row = self._conn.execute("SELECT workspace_uri FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is not None:
                return row["workspace_uri"]
        root = self.path.parent if str(self.path) != ":memory:" else Path.cwd()
        return f"file://{root / 'projects' / '_conversations' / conversation_id / 'workspace'}"

    def _rebuild_runs_table(self) -> None:
        rows = [dict(row) for row in self._conn.execute("SELECT * FROM runs").fetchall()]
        self._conn.execute("DROP TABLE runs")
        self._conn.execute(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
                conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
                org_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT NOT NULL DEFAULT 'default',
                kind TEXT,
                status TEXT NOT NULL,
                execution TEXT NOT NULL DEFAULT 'local',
                workspace_uri TEXT NOT NULL,
                state_json TEXT,
                output_text TEXT NOT NULL DEFAULT '',
                error_json TEXT,
                lease_owner TEXT,
                lease_expires_at INTEGER,
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                completed_at INTEGER,
                updated_at INTEGER NOT NULL
            )
            """
        )
        for row in rows:
            self._conn.execute(
                """
                INSERT INTO runs(
                    id, project_id, conversation_id, org_id, user_id, kind, status,
                    execution, workspace_uri, state_json, output_text, error_json,
                    lease_owner, lease_expires_at, created_at, started_at, completed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row.get("project_id"),
                    row.get("conversation_id"),
                    row.get("org_id", "default"),
                    row.get("user_id", "default"),
                    row.get("kind"),
                    row["status"],
                    row.get("execution", "local"),
                    row["workspace_uri"],
                    row.get("state_json"),
                    row.get("output_text", ""),
                    row.get("error_json"),
                    row.get("lease_owner"),
                    row.get("lease_expires_at"),
                    row["created_at"],
                    row.get("started_at"),
                    row.get("completed_at"),
                    row["updated_at"],
                ),
            )

    def _rebuild_run_streams_table(self) -> None:
        rows = [dict(row) for row in self._conn.execute("SELECT * FROM run_streams").fetchall()]
        self._conn.execute("DROP TABLE run_streams")
        self._conn.execute(
            """
            CREATE TABLE run_streams (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
                conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
                org_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT NOT NULL DEFAULT 'default',
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                completed_at INTEGER,
                error_json TEXT
            )
            """
        )
        for row in rows:
            self._conn.execute(
                """
                INSERT INTO run_streams(
                    id, run_id, project_id, conversation_id, org_id, user_id,
                    kind, status, started_at, completed_at, error_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["run_id"],
                    row.get("project_id"),
                    row.get("conversation_id"),
                    row.get("org_id", "default"),
                    row.get("user_id", "default"),
                    row["kind"],
                    row["status"],
                    row["started_at"],
                    row.get("completed_at"),
                    row.get("error_json"),
                ),
            )

    def _rebuild_reviews_table(self) -> None:
        rows = [dict(row) for row in self._conn.execute("SELECT * FROM reviews").fetchall()]
        self._conn.execute("DROP TABLE reviews")
        self._conn.execute(
            """
            CREATE TABLE reviews (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
                org_id TEXT NOT NULL DEFAULT 'default',
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                decision_json TEXT,
                created_at INTEGER NOT NULL,
                resolved_at INTEGER
            )
            """
        )
        for row in rows:
            self._conn.execute(
                """
                INSERT INTO reviews(
                    id, run_id, project_id, org_id, kind, status,
                    decision_json, created_at, resolved_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["run_id"],
                    row.get("project_id"),
                    row.get("org_id", "default"),
                    row["kind"],
                    row["status"],
                    row.get("decision_json"),
                    row["created_at"],
                    row.get("resolved_at"),
                ),
            )

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
    ) -> Conversation:
        now = _now()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO conversations(
                        id, project_id, org_id, title, status, workspace_uri, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (conversation_id, project_id, org_id, title, workspace_uri, now, now),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise StorageConflictError(str(exc)) from exc
            row = self._required_row("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        return _conversation_from_row(row)

    def list_conversations(self, project_id: str | None = None, *, org_id: str | None = None) -> list[Conversation]:
        conditions = ["archived_at IS NULL"]
        params: list[object] = []
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
            self._conversation_locks.pop(conversation_id, None)
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

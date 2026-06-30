CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL DEFAULT 'default',
    owner_user_id TEXT NOT NULL DEFAULT 'default',
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    workspace_uri TEXT NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    archived_at INTEGER,
    UNIQUE(org_id, slug)
);

CREATE TABLE IF NOT EXISTS conversations (
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
);

CREATE INDEX IF NOT EXISTS idx_conversations_project_updated
    ON conversations(project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_locks (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    owner TEXT NOT NULL,
    acquired_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
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
);

CREATE INDEX IF NOT EXISTS idx_runs_project_updated
    ON runs(project_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_runs_conversation_updated
    ON runs(conversation_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_runs_queued
    ON runs(status, created_at)
    WHERE status = 'queued';

CREATE TABLE IF NOT EXISTS run_streams (
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
);

CREATE INDEX IF NOT EXISTS idx_run_streams_run_started
    ON run_streams(run_id, started_at);

CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL,
    stream_id TEXT NOT NULL,
    stream_seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(run_id, event_id),
    UNIQUE(run_id, stream_id, stream_seq)
);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    org_id TEXT NOT NULL DEFAULT 'default',
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    decision_json TEXT,
    created_at INTEGER NOT NULL,
    resolved_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_reviews_pending
    ON reviews(project_id, status, created_at DESC);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES (1, strftime('%s', 'now'));

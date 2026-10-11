-- Portable subset of SQL: runs on PostgreSQL (production) and SQLite (local dev/tests only).
-- Timestamps are unix epoch seconds; JSON payloads are TEXT validated by pydantic in the app.
CREATE TABLE IF NOT EXISTS candidates (
    video_id      TEXT PRIMARY KEY,
    data          TEXT NOT NULL,
    format_status TEXT NOT NULL,
    ai_likelihood DOUBLE PRECISION,
    fetched_at    DOUBLE PRECISION NOT NULL,
    expires_at    DOUBLE PRECISION NOT NULL,
    used_by_job   TEXT
);
CREATE TABLE IF NOT EXISTS discovery_runs (
    id            TEXT PRIMARY KEY,
    scope         TEXT NOT NULL,
    candidate_ids TEXT NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL,
    expires_at    DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    idem_key     TEXT NOT NULL UNIQUE,
    state        TEXT NOT NULL,
    resume_state TEXT,
    reason       TEXT,
    video_id     TEXT,
    dry_run      INTEGER NOT NULL,
    version      INTEGER NOT NULL DEFAULT 0,
    created_at   DOUBLE PRECISION NOT NULL,
    updated_at   DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs (state);
CREATE TABLE IF NOT EXISTS stage_results (
    job_id     TEXT NOT NULL,
    stage      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (job_id, stage)
);
-- Every billable or side-effecting external call. status: pending|submitted|succeeded|failed|unknown
CREATE TABLE IF NOT EXISTS external_ops (
    op_key       TEXT PRIMARY KEY,
    job_id       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,
    request_id   TEXT,
    artifact_key TEXT,
    detail       TEXT,
    created_at   DOUBLE PRECISION NOT NULL,
    updated_at   DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_ledger (
    day    TEXT NOT NULL,
    bucket TEXT NOT NULL,
    used   INTEGER NOT NULL,
    PRIMARY KEY (day, bucket)
);
CREATE TABLE IF NOT EXISTS uploads (
    job_id          TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    session_uri_enc TEXT,
    file_key        TEXT NOT NULL,
    file_size       BIGINT NOT NULL,
    request         TEXT NOT NULL,
    video_id        TEXT,
    detail          TEXT,
    created_at      DOUBLE PRECISION NOT NULL,
    updated_at      DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id         TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL,
    ts         DOUBLE PRECISION NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    note       TEXT
);
CREATE TABLE IF NOT EXISTS locks (
    name  TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    until DOUBLE PRECISION NOT NULL
);

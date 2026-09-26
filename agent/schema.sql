-- pii-agent/agent/schema.sql  (agent-internal; contract v1A.1)
-- Rule: NO column holds extracted text, OCR text or raw identifier values.
-- last_error holds "<ExceptionClass>: <message truncated to 200 chars>" and never file content.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS kv (                   -- device_id, agent_version, files_scanned_total
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS commands (
    command_id    TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    ack_status    TEXT NOT NULL,                  -- accepted|rejected (re-sent verbatim on redelivery)
    ack_reason    TEXT,
    received_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    scan_id         TEXT PRIMARY KEY,             -- server scan_id, or 'local-<uuid4>' for CLI scans
    command_id      TEXT,                         -- NULL for CLI scans (no progress/complete calls)
    roots_json      TEXT NOT NULL,
    force           INTEGER NOT NULL DEFAULT 0,
    include_types   TEXT NOT NULL,                -- JSON list
    exclude_dirs    TEXT NOT NULL,                -- JSON list
    max_file_mb     INTEGER NOT NULL,
    status          TEXT NOT NULL,                -- queued|running|completed|failed
    crawl_complete  INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      TEXT,
    finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id         TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    file_hash       TEXT,
    file_type       TEXT NOT NULL,
    folder_class    TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    modified_at     TEXT NOT NULL,
    status          TEXT NOT NULL,                -- discovered|processing|done|unscannable|skipped|failed
    status_reason   TEXT,                         -- StatusReason, or 'unchanged' for skipped
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    findings_count  INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    UNIQUE (scan_id, file_path)
);
CREATE INDEX IF NOT EXISTS ix_files_status ON files(status, id);
CREATE INDEX IF NOT EXISTS ix_files_path_hash ON files(file_path, file_hash, status);

CREATE TABLE IF NOT EXISTS findings (             -- copy of DetectResponse.findings (metadata only)
    finding_id        TEXT PRIMARY KEY,
    scan_id           TEXT,
    file_path         TEXT NOT NULL,
    file_hash         TEXT NOT NULL,
    location          TEXT NOT NULL,
    finding_kind      TEXT NOT NULL,
    category          TEXT NOT NULL,
    pii_type          TEXT,
    doc_type          TEXT,
    masked_value      TEXT,
    value_hash        TEXT,
    holder            TEXT NOT NULL,
    confidence        REAL NOT NULL,
    decided_by        TEXT NOT NULL,
    reason            TEXT NOT NULL,
    sensitivity_tier  TEXT NOT NULL,
    tier_reason       TEXT NOT NULL,
    risk_score        REAL NOT NULL,
    risk_band         TEXT NOT NULL,
    detected_at       TEXT NOT NULL
);

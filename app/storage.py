"""SQLite storage shared by the mock portal, the workbench and the executor.

Two schemas live in one database file:
  portal_*      the fictional external portal (step-by-step data entry)
  wb_*          the collection workbench (queue, attempts, conflicts)

The portal schema deliberately has no foreign keys or links to workbench rows,
mirroring a system that knows nothing about the collectors.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_ENV = "RURAL_PORTAL_DB"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "portal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS portal_records (
    record_key     TEXT PRIMARY KEY,
    full_name      TEXT NOT NULL DEFAULT '',
    phone          TEXT NOT NULL DEFAULT '',
    address        TEXT NOT NULL DEFAULT '',
    service_level  TEXT NOT NULL DEFAULT '',
    note           TEXT NOT NULL DEFAULT '',
    revision       INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT,
    updated_by     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS portal_revisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    record_key     TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    source         TEXT NOT NULL,
    operator       TEXT NOT NULL DEFAULT '',
    session_token  TEXT NOT NULL DEFAULT '',
    changed_fields TEXT NOT NULL DEFAULT '{}',
    snapshot       TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    UNIQUE(record_key, revision)
);

CREATE TABLE IF NOT EXISTS portal_sessions (
    token          TEXT PRIMARY KEY,
    record_key     TEXT NOT NULL,
    expected_ver   INTEGER NOT NULL,
    sub_token      TEXT,
    draft          TEXT,
    status         TEXT NOT NULL DEFAULT 'draft',
    result         TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    submitted_at   TEXT,
    archived_at    TEXT
);

CREATE TABLE IF NOT EXISTS wb_batches (
    id             SERIAL,
    name           TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    row_count      INTEGER NOT NULL DEFAULT 0
);
"""

# SQLite has no SERIAL; declared separately for clarity.
SCHEMA = SCHEMA.replace("SERIAL", "INTEGER PRIMARY KEY AUTOINCREMENT")

WB_SCHEMA = """
CREATE TABLE IF NOT EXISTS wb_rows (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id                INTEGER NOT NULL,
    row_number              INTEGER NOT NULL,
    record_key              TEXT NOT NULL,
    target                  TEXT NOT NULL,
    base_snapshot           TEXT,
    base_revision           INTEGER,
    status                  TEXT NOT NULL,
    compare_status          TEXT NOT NULL DEFAULT 'unchecked',
    error_summary           TEXT NOT NULL DEFAULT '',
    last_attempt_at         TEXT,
    last_error              TEXT,
    last_verification       TEXT,
    last_verified_at        TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE(batch_id, row_number)
);

CREATE TABLE IF NOT EXISTS wb_attempts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    row_id                  INTEGER NOT NULL,
    attempt_no              INTEGER NOT NULL,
    status                  TEXT NOT NULL,
    stage                   TEXT NOT NULL DEFAULT '',
    http_status             INTEGER,
    error_kind              TEXT,
    error_message           TEXT,
    form_token              TEXT,
    sub_token               TEXT,
    base_snapshot           TEXT,
    target                  TEXT NOT NULL,
    verify_verdict          TEXT,
    verify_detail           TEXT,
    needs_manual            INTEGER NOT NULL DEFAULT 0,
    next_retry_at           TEXT,
    started_at              TEXT NOT NULL,
    finished_at             TEXT,
    UNIQUE(row_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS wb_conflicts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    row_id                  INTEGER NOT NULL,
    reason                  TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'open',
    verify_detail           TEXT NOT NULL DEFAULT '{}',
    current_snapshot        TEXT NOT NULL DEFAULT '{}',
    opened_at               TEXT NOT NULL,
    resolved_by             TEXT,
    resolved_at             TEXT,
    resolution_note         TEXT
);

CREATE TABLE IF NOT EXISTS wb_takeovers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    row_id                  INTEGER NOT NULL,
    operator                TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'active',
    revoked_leases          INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL,
    released_at             TEXT
);

CREATE TABLE IF NOT EXISTS wb_jobs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                    TEXT NOT NULL,
    row_id                  INTEGER,
    status                  TEXT NOT NULL,
    payload                 TEXT NOT NULL DEFAULT '{}',
    result                  TEXT NOT NULL DEFAULT '{}',
    fault_profile           TEXT NOT NULL DEFAULT 'cm-default',
    started_at              TEXT,
    finished_at             TEXT,
    heartbeat_at            TEXT
);

CREATE TABLE IF NOT EXISTS wb_executors (
    id                      TEXT PRIMARY KEY,
    job_id                  INTEGER,
    row_id                  INTEGER,
    pid                     INTEGER,
    status                  TEXT NOT NULL,
    started_at              TEXT NOT NULL,
    heartbeat_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    at                      TEXT NOT NULL,
    actor                   TEXT NOT NULL,
    action                  TEXT NOT NULL,
    detail                  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS wb_settings (
    key                     TEXT PRIMARY KEY,
    value                   TEXT NOT NULL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_portal_sessions_record ON portal_sessions(record_key);
CREATE INDEX IF NOT EXISTS idx_portal_revisions_record ON portal_revisions(record_key);
CREATE INDEX IF NOT EXISTS idx_wb_rows_batch ON wb_rows(batch_id);
CREATE INDEX IF NOT EXISTS idx_wb_rows_status ON wb_rows(status);
CREATE INDEX IF NOT EXISTS idx_wb_attempts_row ON wb_attempts(row_id);
CREATE INDEX IF NOT EXISTS idx_wb_events_at ON wb_events(at);
CREATE INDEX IF NOT EXISTS idx_wb_executors_status ON wb_executors(status);
CREATE INDEX IF NOT EXISTS idx_wb_conflicts_status ON wb_conflicts(status);
CREATE INDEX IF NOT EXISTS idx_wb_takeovers_row ON wb_takeovers(row_id);
"""


def db_path() -> Path:
    return Path(os.environ.get(DB_ENV) or DEFAULT_DB)


def connect(db: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db) if db else db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection, portal_seed: bool = True) -> None:
    conn.executescript(SCHEMA)
    conn.executescript(WB_SCHEMA)
    conn.executescript(INDEXES)
    _migrate(conn)
    if portal_seed:
        from .portal.seed import ensure_seed

        ensure_seed(conn)


# Columns added after the first schema draft; kept for old local databases.
MIGRATIONS = {
    "portal_revisions": {"session_token": "TEXT NOT NULL DEFAULT ''"},
    "wb_attempts": {"form_token": "TEXT"},
    "wb_rows": {"last_verification": "TEXT", "last_verified_at": "TEXT"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def event(conn: sqlite3.Connection, actor: str, action: str, detail: str = "") -> None:
    from .common import iso

    conn.execute(
        "INSERT INTO wb_events(at, actor, action, detail) VALUES(?,?,?,?)",
        (iso(), actor, action, detail),
    )


def wb_reset(conn: sqlite3.Connection) -> None:
    """Clear all workbench batches/attempts/conflicts (drill only)."""
    for table in (
        "wb_conflicts", "wb_takeovers", "wb_attempts", "wb_rows", "wb_batches",
        "wb_jobs", "wb_executors", "wb_events",
    ):
        conn.execute(f"DELETE FROM {table}")
    event(conn, "workbench", "reset", "清空工作台演练数据")


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM wb_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO wb_settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )

"""SQLite schema bootstrap (T02).

Tables: ``tasks``, ``workstations``, ``task_results``, ``error_logs``.

The schema mirrors the contracts in docs/data-and-storage.md:

* ``task_id`` is the primary key on tasks; ``(creative_id, segment_id)`` is a
  business-level uniqueness rule enforced by the importer & scheduler at
  application layer (so historical / failed rows can stay around).
* ``workstations`` carries ``stats_date`` so cross-day counter resets can be
  done atomically inside the scheduler transaction.
* ``task_results`` carries ``generation_round`` and ``sequence_no`` so we can
  trace each candidate file back to its round, and a UNIQUE constraint on
  ``(task_id, generation_round, sequence_no)`` prevents accidental double
  inserts during recovery (T17).
"""

from __future__ import annotations

from pathlib import Path

from app.db.connection import connect


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id                TEXT PRIMARY KEY,
    sku_id                 TEXT NOT NULL,
    creative_id            TEXT NOT NULL,
    segment_id             TEXT NOT NULL,
    sequence_index         INTEGER NOT NULL DEFAULT 0,
    source_asset_path      TEXT NOT NULL,
    source_asset_type      TEXT NOT NULL CHECK (
        source_asset_type IN ('first_frame', 'last_frame',
                              'previous_segment_frame', 'reference', 'other')
    ),
    video_prompt           TEXT NOT NULL,
    target_count           INTEGER NOT NULL CHECK (target_count > 0),
    downloaded_count       INTEGER NOT NULL DEFAULT 0,
    generation_round_count INTEGER NOT NULL DEFAULT 0,
    generated_count        INTEGER,
    depends_on_task_id     TEXT,
    status                 TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending', 'running', 'success', 'retry_waiting',
            'failed', 'download_failed', 'manual_review'
        )
    ),
    assigned_workstation_id TEXT,
    retry_count            INTEGER NOT NULL DEFAULT 0,
    max_retry_count        INTEGER NOT NULL DEFAULT 2,
    zombie_recovery_count  INTEGER NOT NULL DEFAULT 0,
    result_folder          TEXT,
    error_type             TEXT,
    error_message          TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    started_at             TEXT,
    finished_at            TEXT,
    FOREIGN KEY (depends_on_task_id) REFERENCES tasks(task_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_creative_segment_status
    ON tasks (creative_id, segment_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status_created
    ON tasks (status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_assigned_ws
    ON tasks (assigned_workstation_id);

CREATE TABLE IF NOT EXISTS workstations (
    id                          TEXT PRIMARY KEY,
    account_label               TEXT NOT NULL,
    browser_profile_path        TEXT NOT NULL,
    daily_task_limit            INTEGER NOT NULL CHECK (daily_task_limit > 0),
    status                      TEXT NOT NULL DEFAULT 'healthy' CHECK (
        status IN ('healthy', 'busy', 'cooldown', 'manual_check',
                   'nurturing', 'disabled')
    ),
    stats_date                  TEXT,
    today_success_count         INTEGER NOT NULL DEFAULT 0,
    today_failure_count         INTEGER NOT NULL DEFAULT 0,
    consecutive_failure_count   INTEGER NOT NULL DEFAULT 0,
    last_success_at             TEXT,
    last_failure_at             TEXT,
    cooldown_until              TEXT,
    cooldown_reason             TEXT,
    ban_probe_count             INTEGER NOT NULL DEFAULT 0,
    manual_note                 TEXT,
    -- Flow project + UI preset (was in workstations.yaml; now editable
    -- from the Web UI form so the customer never touches yaml).
    flow_project_url            TEXT,
    flow_mode_tab               TEXT,
    flow_mode_subtab            TEXT,
    flow_mode_aspect            TEXT,
    flow_mode_output_count      INTEGER,
    flow_mode_duration_sec      INTEGER,
    flow_mode_model             TEXT,
    updated_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           TEXT NOT NULL,
    creative_id       TEXT NOT NULL,
    segment_id        TEXT NOT NULL,
    workstation_id    TEXT,
    generation_round  INTEGER NOT NULL,
    sequence_no       INTEGER NOT NULL,
    video_file_path   TEXT NOT NULL,
    screenshot_path   TEXT,
    status            TEXT NOT NULL DEFAULT 'downloaded',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (task_id, generation_round, sequence_no),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_task_results_task
    ON task_results (task_id);
CREATE INDEX IF NOT EXISTS idx_task_results_creative_segment
    ON task_results (creative_id, segment_id);

CREATE TABLE IF NOT EXISTS error_logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           TEXT,
    workstation_id    TEXT,
    generation_round  INTEGER,
    error_type        TEXT NOT NULL,
    error_message     TEXT,
    screenshot_path   TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_error_logs_task
    ON error_logs (task_id);
CREATE INDEX IF NOT EXISTS idx_error_logs_ws
    ON error_logs (workstation_id);
CREATE INDEX IF NOT EXISTS idx_error_logs_created
    ON error_logs (created_at);

-- One-to-many: a task may carry multiple ordered source assets (e.g.
-- first_frame + last_frame for Veo frame-to-frame mode, or several
-- reference images for Ingredients mode). When a task has rows here,
-- the worker uploads them in ``asset_order`` ASC. ``tasks.source_asset_path``
-- is kept for the legacy single-asset case (importer mirrors the first
-- asset there for backward compatibility with existing reports / queries).
CREATE TABLE IF NOT EXISTS task_assets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    asset_order  INTEGER NOT NULL,
    asset_path   TEXT NOT NULL,
    asset_type   TEXT NOT NULL CHECK (
        asset_type IN ('first_frame', 'last_frame',
                       'previous_segment_frame', 'reference', 'other')
    ),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (task_id, asset_order),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_task_assets_task_order
    ON task_assets (task_id, asset_order);

-- Single-row key/value store for runtime app settings the operator
-- toggles from the Web UI (currently: ``operation_mode`` = day | night).
-- Plain text values; callers parse / validate.
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_schema(db_path: Path | str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        _migrate(conn)
        _widen_workstations_status_check(conn)
    finally:
        conn.close()


def _widen_workstations_status_check(conn) -> None:
    """Idempotent widening of the workstations.status CHECK constraint to
    include the ``nurturing`` state introduced in 2026-05.

    SQLite has no ``ALTER TABLE ... DROP CONSTRAINT``; the documented
    workaround is rewriting ``sqlite_master.sql`` under
    ``PRAGMA writable_schema = ON``. Safe here because (a) we don't touch
    column definitions, only the literal CHECK string, and (b) every
    existing row already satisfies the wider constraint.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='workstations'"
    ).fetchone()
    if row is None:
        return
    sql = row[0] or ""
    if "'nurturing'" in sql:
        return
    old_check = (
        "status IN ('healthy', 'busy', 'cooldown', 'manual_check', 'disabled')"
    )
    new_check = (
        "status IN ('healthy', 'busy', 'cooldown', 'manual_check', "
        "'nurturing', 'disabled')"
    )
    if old_check not in sql:
        return  # constraint shape unexpected; bail rather than corrupt schema
    new_sql = sql.replace(old_check, new_check)
    conn.execute("PRAGMA writable_schema = ON")
    try:
        conn.execute(
            "UPDATE sqlite_master SET sql = ? "
            "WHERE type='table' AND name='workstations'",
            (new_sql,),
        )
        conn.commit()
    finally:
        conn.execute("PRAGMA writable_schema = OFF")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    if integrity[0] != "ok":
        raise RuntimeError(
            f"integrity_check failed after widening status CHECK: {integrity[0]}"
        )


_MIGRATIONS: list[tuple[str, str]] = [
    # Per-column ALTER TABLE migrations. Re-running on a DB that already
    # has the column raises "duplicate column name" — caught and ignored.
    ("workstations", "ban_probe_count INTEGER NOT NULL DEFAULT 0"),
    # DB-as-source-of-truth for flow_project_url + flow_mode.
    ("workstations", "flow_project_url TEXT"),
    ("workstations", "flow_mode_tab TEXT"),
    ("workstations", "flow_mode_subtab TEXT"),
    ("workstations", "flow_mode_aspect TEXT"),
    ("workstations", "flow_mode_output_count INTEGER"),
    ("workstations", "flow_mode_duration_sec INTEGER"),
    ("workstations", "flow_mode_model TEXT"),
    # Per-task flow_mode overrides — any column left NULL falls back to
    # the workstation's preset at runtime.
    ("tasks", "flow_mode_tab TEXT"),
    ("tasks", "flow_mode_subtab TEXT"),
    ("tasks", "flow_mode_aspect TEXT"),
    ("tasks", "flow_mode_output_count INTEGER"),
    ("tasks", "flow_mode_duration_sec INTEGER"),
    ("tasks", "flow_mode_model TEXT"),
    # How many times the scheduler has auto-resumed this task after a
    # retry-budget exhaustion. Capped to prevent infinite loops when
    # Flow's unusual_activity fence is sticky; a manual "继续任务" click
    # resets the counter so the customer regains a fresh budget.
    ("tasks", "auto_resumed_count INTEGER NOT NULL DEFAULT 0"),
]


def _migrate(conn) -> None:
    import sqlite3
    for table, column_def in _MIGRATIONS:
        col_name = column_def.split()[0]
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    conn.commit()

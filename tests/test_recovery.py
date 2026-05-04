"""T17 — recovery / idempotency tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config.loader import RecoverySettings
from app.db.connection import connect
from app.scheduler.recovery import recover_zombie_tasks
from app.workstations.sync import sync_workstations


def _seed_running(conn, *, task_id: str, started_minutes_ago: int,
                  ws_id="WS_A", retry_count=0, zombie_count=0,
                  max_retry=2) -> None:
    started = (datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
        "source_asset_path, source_asset_type, video_prompt, target_count, "
        "status, retry_count, max_retry_count, assigned_workstation_id, "
        "started_at, zombie_recovery_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)",
        (task_id, "sku", "cre", "A", "/x", "first_frame", "p", 8,
         retry_count, max_retry, ws_id, started, zombie_count),
    )
    conn.execute("UPDATE workstations SET status='busy' WHERE id = ?", (ws_id,))


def _cfg() -> RecoverySettings:
    return RecoverySettings(running_stale_minutes=30, zombie_recovery_limit=3)


def test_stale_running_task_revived_to_retry_waiting(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed_running(conn, task_id="T1", started_minutes_ago=60)
        s = recover_zombie_tasks(conn, cfg=_cfg())
        assert s.revived == 1
        assert s.escalated_manual == 0
        row = conn.execute("SELECT status, retry_count, zombie_recovery_count "
                           "FROM tasks WHERE task_id='T1'").fetchone()
        assert row["status"] == "retry_waiting"
        assert row["retry_count"] == 1
        assert row["zombie_recovery_count"] == 1
        ws = conn.execute("SELECT status FROM workstations WHERE id='WS_A'").fetchone()
        assert ws["status"] == "healthy"
    finally:
        conn.close()


def test_fresh_running_task_not_recovered(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed_running(conn, task_id="T1", started_minutes_ago=5)
        s = recover_zombie_tasks(conn, cfg=_cfg())
        assert s.revived == 0
        row = conn.execute("SELECT status FROM tasks WHERE task_id='T1'").fetchone()
        assert row["status"] == "running"
    finally:
        conn.close()


def test_zombie_recovery_limit_escalates_to_manual_review(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        # Already 2 zombie recoveries; one more should escalate.
        _seed_running(conn, task_id="T1", started_minutes_ago=60, zombie_count=2)
        s = recover_zombie_tasks(conn, cfg=_cfg())
        assert s.revived == 0
        assert s.escalated_manual == 1
        row = conn.execute("SELECT status, zombie_recovery_count FROM tasks WHERE task_id='T1'").fetchone()
        assert row["status"] == "manual_review"
        assert row["zombie_recovery_count"] == 3
    finally:
        conn.close()


def test_startup_cleanup_resets_running_regardless_of_age(
    db_path: Path, workstations,
) -> None:
    """Process-boot cleanup ignores running_stale_minutes — every
    running task in a fresh process is by definition orphaned."""
    from app.scheduler.recovery import reset_zombie_state_on_startup

    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        # Started 30 SECONDS ago — far below the 30-min mid-loop
        # threshold, but still orphaned from a customer-side restart.
        _seed_running(conn, task_id="T_RECENT", started_minutes_ago=0)
        s = reset_zombie_state_on_startup(conn)
        assert s.revived == 1
        assert s.escalated_manual == 0
        row = conn.execute(
            "SELECT status, retry_count, zombie_recovery_count, "
            "assigned_workstation_id, started_at, error_type "
            "FROM tasks WHERE task_id='T_RECENT'"
        ).fetchone()
        assert row["status"] == "retry_waiting"
        assert row["zombie_recovery_count"] == 1
        # Startup cleanup does NOT bump retry_count — it's not a
        # task-side error, just a process restart.
        assert row["retry_count"] == 0
        assert row["assigned_workstation_id"] is None
        assert row["started_at"] is None
        assert row["error_type"] == "internal"
        ws = conn.execute(
            "SELECT status FROM workstations WHERE id='WS_A'"
        ).fetchone()
        assert ws["status"] == "healthy"
    finally:
        conn.close()


def test_startup_cleanup_escalates_repeat_zombies(
    db_path: Path, workstations,
) -> None:
    """A task that gets orphaned 3 times in a row (zombie_recovery_count
    reaches the limit) goes to manual_review instead of retry_waiting."""
    from app.scheduler.recovery import reset_zombie_state_on_startup

    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed_running(
            conn, task_id="T_REPEAT", started_minutes_ago=0,
            zombie_count=2,  # one more bump → limit (3) reached
        )
        s = reset_zombie_state_on_startup(conn)
        assert s.revived == 0
        assert s.escalated_manual == 1
        row = conn.execute(
            "SELECT status FROM tasks WHERE task_id='T_REPEAT'"
        ).fetchone()
        assert row["status"] == "manual_review"
    finally:
        conn.close()


def test_startup_cleanup_releases_busy_workstations(
    db_path: Path, workstations,
) -> None:
    """Even if there's no running task, a workstation stuck in busy
    (e.g. claim then crash before task insert) gets released."""
    from app.scheduler.recovery import reset_zombie_state_on_startup

    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        conn.execute("UPDATE workstations SET status='busy' WHERE id='WS_A'")
        s = reset_zombie_state_on_startup(conn)
        assert s.revived == 0
        ws = conn.execute(
            "SELECT status FROM workstations WHERE id='WS_A'"
        ).fetchone()
        assert ws["status"] == "healthy"
    finally:
        conn.close()


def test_startup_cleanup_noop_on_clean_db(
    db_path: Path, workstations,
) -> None:
    """Idempotent — if there are no running tasks / busy workstations,
    cleanup just reports zeros and changes nothing."""
    from app.scheduler.recovery import reset_zombie_state_on_startup

    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        s = reset_zombie_state_on_startup(conn)
        assert s.revived == 0
        assert s.escalated_manual == 0
    finally:
        conn.close()


def test_task_results_unique_prevents_double_insert(db_path: Path, workstations) -> None:
    """Recovery scenario: re-running a round must not duplicate results."""
    import sqlite3

    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
            "source_asset_path, source_asset_type, video_prompt, target_count) "
            "VALUES ('T1', 'sku', 'cre', 'A', '/x', 'first_frame', 'p', 4)"
        )
        conn.execute(
            "INSERT INTO task_results (task_id, creative_id, segment_id, "
            "workstation_id, generation_round, sequence_no, video_file_path) "
            "VALUES ('T1', 'cre', 'A', 'WS_A', 1, 1, '/v.mp4')"
        )
        try:
            conn.execute(
                "INSERT INTO task_results (task_id, creative_id, segment_id, "
                "workstation_id, generation_round, sequence_no, video_file_path) "
                "VALUES ('T1', 'cre', 'A', 'WS_A', 1, 1, '/v2.mp4')"
            )
            assert False, "expected IntegrityError"
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()

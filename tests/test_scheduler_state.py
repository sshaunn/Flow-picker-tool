"""T13 / T14 — task state machine and workstation cooldown."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config.loader import CooldownSettings
from app.db.connection import connect, transaction
from app.scheduler.state import (
    finalize_task,
    force_manual_check,
    probe_recover_banned_workstations,
    recover_workstation_states,
    release_orphaned_busy_workstations,
)
from app.workstations.sync import sync_workstations


def _ws_status(conn, ws_id: str) -> str:
    return conn.execute("SELECT status FROM workstations WHERE id = ?", (ws_id,)).fetchone()["status"]


def _seed_running_task(conn, *, task_id="T1", retry_count=0, ws_id="WS_A", max_retry=2) -> None:
    conn.execute(
        "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
        "source_asset_path, source_asset_type, video_prompt, target_count, "
        "status, retry_count, max_retry_count, assigned_workstation_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)",
        (task_id, "sku", "cre", "A", "/x", "first_frame", "p", 8,
         retry_count, max_retry, ws_id),
    )
    conn.execute("UPDATE workstations SET status='busy' WHERE id = ?", (ws_id,))


def _cfg() -> CooldownSettings:
    return CooldownSettings(
        consecutive_failure_threshold=3,
        cooldown_duration_short_min=30,
        cooldown_duration_long_min=60,
        page_failure_window_min=5,
        page_failure_threshold=3,
    )


def test_running_to_success_does_not_increment_retry(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed_running_task(conn, retry_count=1)
        with transaction(conn):
            finalize_task(
                conn, cooldown_cfg=_cfg(),
                task_id="T1", workstation_id="WS_A",
                final_status="success", downloaded_count=8,
                generation_round_count=2, last_error_type=None, last_error_message=None,
                workstation_outcome="healthy", result_folder="/x",
            )
        row = conn.execute("SELECT retry_count, status FROM tasks WHERE task_id='T1'").fetchone()
        assert row["status"] == "success"
        assert row["retry_count"] == 1
        assert _ws_status(conn, "WS_A") == "healthy"
    finally:
        conn.close()


def test_running_to_retry_waiting_increments_retry(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed_running_task(conn, retry_count=0)
        with transaction(conn):
            finalize_task(
                conn, cooldown_cfg=_cfg(),
                task_id="T1", workstation_id="WS_A",
                final_status="retry_waiting", downloaded_count=2,
                generation_round_count=1,
                last_error_type="unusual_activity", last_error_message="x",
                workstation_outcome="manual_check", result_folder=None,
            )
        row = conn.execute("SELECT retry_count, status FROM tasks WHERE task_id='T1'").fetchone()
        assert row["status"] == "retry_waiting"
        assert row["retry_count"] == 1
        # First unusual_activity strike goes to cooldown (not manual_check)
        # so the next claim cycle can let the WS try again.
        assert _ws_status(conn, "WS_A") == "cooldown"
    finally:
        conn.close()


def test_running_to_failed_does_not_increment_retry(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed_running_task(conn, retry_count=2)
        with transaction(conn):
            finalize_task(
                conn, cooldown_cfg=_cfg(),
                task_id="T1", workstation_id="WS_A",
                final_status="failed", downloaded_count=4,
                generation_round_count=2,
                last_error_type=None, last_error_message=None,
                workstation_outcome="healthy", result_folder=None,
            )
        row = conn.execute("SELECT retry_count, status FROM tasks WHERE task_id='T1'").fetchone()
        assert row["status"] == "failed"
        assert row["retry_count"] == 2
    finally:
        conn.close()


def test_running_to_download_failed_does_not_increment_retry(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed_running_task(conn, retry_count=0)
        with transaction(conn):
            finalize_task(
                conn, cooldown_cfg=_cfg(),
                task_id="T1", workstation_id="WS_A",
                final_status="download_failed", downloaded_count=4,
                generation_round_count=1,
                last_error_type="download_failed", last_error_message="x",
                workstation_outcome="healthy", result_folder=None,
            )
        row = conn.execute("SELECT retry_count, status FROM tasks WHERE task_id='T1'").fetchone()
        assert row["retry_count"] == 0
    finally:
        conn.close()


def test_consecutive_failure_triggers_cooldown(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        # Three consecutive page_failure outcomes -> cooldown
        for i in range(3):
            _seed_running_task(conn, task_id=f"T{i+1}")
            with transaction(conn):
                finalize_task(
                    conn, cooldown_cfg=_cfg(),
                    task_id=f"T{i+1}", workstation_id="WS_A",
                    final_status="retry_waiting", downloaded_count=0,
                    generation_round_count=1,
                    last_error_type="page_load_failed", last_error_message="x",
                    workstation_outcome="page_failure", result_folder=None,
                )
        ws = conn.execute(
            "SELECT status, cooldown_until, consecutive_failure_count "
            "FROM workstations WHERE id='WS_A'"
        ).fetchone()
        assert ws["status"] == "cooldown"
        assert ws["cooldown_until"] is not None
        assert ws["consecutive_failure_count"] == 3
    finally:
        conn.close()


def test_cooldown_expires_back_to_healthy(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE workstations SET status='cooldown', cooldown_until=?, "
            "cooldown_reason='consecutive_failure', consecutive_failure_count=3 "
            "WHERE id='WS_A'",
            (past,),
        )
        n = recover_workstation_states(conn)
        assert n == 1
        ws = conn.execute(
            "SELECT status, cooldown_until, consecutive_failure_count FROM workstations WHERE id='WS_A'"
        ).fetchone()
        assert ws["status"] == "healthy"
        assert ws["cooldown_until"] is None
        assert ws["consecutive_failure_count"] == 0
    finally:
        conn.close()


def test_release_orphaned_busy_workstation(db_path: Path, workstations) -> None:
    """A workstation stuck in 'busy' with no running task (e.g. after Ctrl+C
    killed the runner mid-claim) must be released by the recovery sweep."""
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        # Stranded: WS_A is busy but no task assigned to it is 'running'.
        conn.execute("UPDATE workstations SET status='busy' WHERE id='WS_A'")
        n = release_orphaned_busy_workstations(conn)
        assert n == 1
        assert _ws_status(conn, "WS_A") == "healthy"
    finally:
        conn.close()


def test_release_orphaned_busy_keeps_legitimate_busy(db_path: Path, workstations) -> None:
    """If a workstation is busy AND has a running task, leave it alone."""
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed_running_task(conn, ws_id="WS_A")  # marks WS_A busy & inserts running task
        n = release_orphaned_busy_workstations(conn)
        assert n == 0
        assert _ws_status(conn, "WS_A") == "busy"
    finally:
        conn.close()


def test_force_manual_check_does_not_auto_recover(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        force_manual_check(conn, workstation_id="WS_A", reason="unusual_activity")
        recover_workstation_states(conn)
        assert _ws_status(conn, "WS_A") == "manual_check"
    finally:
        conn.close()


def test_unusual_activity_first_strike_goes_to_cooldown(db_path: Path, workstations) -> None:
    """First unusual_activity hit cools the WS down (not manual_check) so
    the next cron tick can let it retry. The strike counter ticks up."""
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed_running_task(conn, retry_count=0)
        with transaction(conn):
            finalize_task(
                conn,
                cooldown_cfg=_cfg(),
                task_id="T1",
                workstation_id="WS_A",
                final_status="retry_waiting",
                downloaded_count=0,
                generation_round_count=1,
                last_error_type="unusual_activity",
                last_error_message="banned",
                workstation_outcome="manual_check",
                result_folder=None,
            )
        row = conn.execute(
            "SELECT status, cooldown_until, ban_probe_count, cooldown_reason "
            "FROM workstations WHERE id = 'WS_A'"
        ).fetchone()
        assert row["status"] == "cooldown"
        assert row["cooldown_until"] is not None
        assert row["ban_probe_count"] == 1
        assert row["cooldown_reason"] == "unusual_activity_strike_1"
    finally:
        conn.close()


def _hit_unusual_activity(conn, *, ws_id: str, retry_count: int = 0) -> None:
    # Helper: simulate one unusual_activity hit on the given WS.
    # Re-seeds the task as ``running`` each time so finalize_task can
    # transition it.
    conn.execute("DELETE FROM tasks WHERE task_id='T1'")
    _seed_running_task(conn, retry_count=retry_count, ws_id=ws_id)
    with transaction(conn):
        finalize_task(
            conn, cooldown_cfg=_cfg(),
            task_id="T1", workstation_id=ws_id,
            final_status="retry_waiting", downloaded_count=0,
            generation_round_count=1,
            last_error_type="unusual_activity", last_error_message="x",
            workstation_outcome="manual_check", result_folder=None,
        )


def test_unusual_activity_strikes_escalate_to_manual_check(
    db_path: Path, workstations,
) -> None:
    """After ``max_strikes`` (default 5) consecutive unusual_activity
    hits with no success in between, the WS lands in manual_check."""
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        for i in range(1, 5):  # strikes 1..4 → cooldown
            _hit_unusual_activity(conn, ws_id="WS_A")
            row = conn.execute(
                "SELECT status, ban_probe_count FROM workstations "
                "WHERE id='WS_A'"
            ).fetchone()
            assert row["status"] == "cooldown"
            assert row["ban_probe_count"] == i
        # strike 5 → manual_check
        _hit_unusual_activity(conn, ws_id="WS_A")
        row = conn.execute(
            "SELECT status, ban_probe_count FROM workstations WHERE id='WS_A'"
        ).fetchone()
        assert row["status"] == "manual_check"
        # Counter resets so the probe-recovery path can take over.
        assert row["ban_probe_count"] == 0
    finally:
        conn.close()


def test_success_resets_unusual_activity_strike_counter(
    db_path: Path, workstations,
) -> None:
    """A successful generation clears the strike counter so a single
    isolated strike doesn't haunt the WS forever."""
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        # Two strikes → counter at 2.
        _hit_unusual_activity(conn, ws_id="WS_A")
        _hit_unusual_activity(conn, ws_id="WS_A")
        assert conn.execute(
            "SELECT ban_probe_count FROM workstations WHERE id='WS_A'"
        ).fetchone()["ban_probe_count"] == 2
        # WS goes back to running, then succeeds.
        conn.execute("UPDATE workstations SET status='busy' WHERE id='WS_A'")
        conn.execute("DELETE FROM tasks WHERE task_id='T1'")
        _seed_running_task(conn, retry_count=0, ws_id="WS_A")
        with transaction(conn):
            finalize_task(
                conn, cooldown_cfg=_cfg(),
                task_id="T1", workstation_id="WS_A",
                final_status="success", downloaded_count=8,
                generation_round_count=1,
                last_error_type=None, last_error_message=None,
                workstation_outcome="healthy", result_folder=None,
            )
        row = conn.execute(
            "SELECT status, ban_probe_count FROM workstations WHERE id='WS_A'"
        ).fetchone()
        assert row["status"] == "healthy"
        assert row["ban_probe_count"] == 0


    finally:
        conn.close()


def test_login_required_skips_strike_logic(db_path: Path, workstations) -> None:
    """login_required / captcha_or_verification go straight to manual_check
    without strike accumulation — those genuinely need a human."""
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed_running_task(conn, retry_count=0)
        with transaction(conn):
            finalize_task(
                conn, cooldown_cfg=_cfg(),
                task_id="T1", workstation_id="WS_A",
                final_status="retry_waiting", downloaded_count=0,
                generation_round_count=1,
                last_error_type="login_required", last_error_message="x",
                workstation_outcome="manual_check", result_folder=None,
            )
        row = conn.execute(
            "SELECT status, ban_probe_count FROM workstations WHERE id='WS_A'"
        ).fetchone()
        assert row["status"] == "manual_check"
        assert row["ban_probe_count"] == 0
    finally:
        conn.close()


def _set_ws_manual_check(conn, ws_id: str, *, cooldown_until: str | None,
                          ban_probe_count: int = 0) -> None:
    conn.execute(
        "UPDATE workstations SET status='manual_check', cooldown_until=?, "
        "cooldown_reason='unusual_activity', ban_probe_count=? WHERE id=?",
        (cooldown_until, ban_probe_count, ws_id),
    )


def test_probe_recover_clean_returns_to_healthy(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _set_ws_manual_check(conn, "WS_A",
                             cooldown_until="2000-01-01 00:00:00",
                             ban_probe_count=1)
        with transaction(conn):
            stats = probe_recover_banned_workstations(
                conn, cooldown_cfg=_cfg(), probe_fn=lambda _: False,
            )
        assert stats == {"recovered": 1, "still_banned": 0, "exhausted": 0}
        row = conn.execute(
            "SELECT status, cooldown_until, ban_probe_count, cooldown_reason "
            "FROM workstations WHERE id = 'WS_A'"
        ).fetchone()
        assert row["status"] == "healthy"
        assert row["cooldown_until"] is None
        assert row["ban_probe_count"] == 0
        assert row["cooldown_reason"] is None
    finally:
        conn.close()


def test_probe_recover_still_banned_advances_tier(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _set_ws_manual_check(conn, "WS_A",
                             cooldown_until="2000-01-01 00:00:00",
                             ban_probe_count=0)
        with transaction(conn):
            stats = probe_recover_banned_workstations(
                conn, cooldown_cfg=_cfg(), probe_fn=lambda _: True,
            )
        assert stats == {"recovered": 0, "still_banned": 1, "exhausted": 0}
        row = conn.execute(
            "SELECT status, cooldown_until, ban_probe_count "
            "FROM workstations WHERE id = 'WS_A'"
        ).fetchone()
        assert row["status"] == "manual_check"
        assert row["cooldown_until"] is not None  # next tier scheduled
        assert row["ban_probe_count"] == 1
    finally:
        conn.close()


def test_probe_recover_last_tier_exhausts(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        # tier-0 / tier-1 / tier-2 → after 2 failed probes, the next
        # advance is into tier 3 which doesn't exist → exhausted.
        _set_ws_manual_check(conn, "WS_A",
                             cooldown_until="2000-01-01 00:00:00",
                             ban_probe_count=2)
        with transaction(conn):
            stats = probe_recover_banned_workstations(
                conn, cooldown_cfg=_cfg(), probe_fn=lambda _: True,
            )
        assert stats == {"recovered": 0, "still_banned": 0, "exhausted": 1}
        row = conn.execute(
            "SELECT status, cooldown_until, ban_probe_count, cooldown_reason "
            "FROM workstations WHERE id = 'WS_A'"
        ).fetchone()
        assert row["status"] == "manual_check"
        assert row["cooldown_until"] is None  # no more auto-recovery
        assert row["ban_probe_count"] == 3
        assert "exhausted" in (row["cooldown_reason"] or "")
    finally:
        conn.close()


def test_probe_recover_skips_unexpired(db_path: Path, workstations) -> None:
    """A WS with cooldown_until still in the future is not probed."""
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        future = "2999-01-01 00:00:00"
        _set_ws_manual_check(conn, "WS_A", cooldown_until=future)
        probe_calls = []
        with transaction(conn):
            stats = probe_recover_banned_workstations(
                conn, cooldown_cfg=_cfg(),
                probe_fn=lambda ws_id: probe_calls.append(ws_id) or False,
            )
        assert probe_calls == []
        assert stats == {"recovered": 0, "still_banned": 0, "exhausted": 0}
    finally:
        conn.close()


def test_probe_recover_swallows_probe_exception(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _set_ws_manual_check(conn, "WS_A",
                             cooldown_until="2000-01-01 00:00:00",
                             ban_probe_count=0)
        def raising_probe(_ws_id: str) -> bool:
            raise RuntimeError("playwright crashed")
        with transaction(conn):
            stats = probe_recover_banned_workstations(
                conn, cooldown_cfg=_cfg(), probe_fn=raising_probe,
            )
        # Crashed probe must not flip status; counters untouched.
        assert stats == {"recovered": 0, "still_banned": 0, "exhausted": 0}
        row = conn.execute(
            "SELECT status, ban_probe_count FROM workstations WHERE id = 'WS_A'"
        ).fetchone()
        assert row["status"] == "manual_check"
        assert row["ban_probe_count"] == 0
    finally:
        conn.close()

"""T12 — atomic claim / scheduler tests."""

from __future__ import annotations

import threading
from datetime import date, timedelta
from pathlib import Path

from app.db.connection import connect
from app.scheduler.claim import claim_one, reset_stale_stats
from app.workstations.sync import sync_workstations


def _seed(conn, *, status="pending", task_id="T1", creative="cre", segment="A",
          retry_count=0, max_retry=2, depends_on=None) -> None:
    conn.execute(
        "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
        "source_asset_path, source_asset_type, video_prompt, target_count, status, "
        "retry_count, max_retry_count, depends_on_task_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, "sku", creative, segment, "/x", "first_frame", "p", 4,
         status, retry_count, max_retry, depends_on),
    )


def test_claim_picks_pending_and_marks_running(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed(conn)
        claim = claim_one(conn)
        assert claim is not None
        assert claim.task_row["status"] == "running"
        assert claim.task_row["assigned_workstation_id"] == claim.workstation_id
        ws = conn.execute(
            "SELECT status FROM workstations WHERE id = ?", (claim.workstation_id,)
        ).fetchone()
        assert ws["status"] == "busy"
    finally:
        conn.close()


def test_no_task_releases_workstation(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        # No tasks at all -> claim returns None and workstations stay healthy.
        claim = claim_one(conn)
        assert claim is None
        states = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM workstations")}
        assert all(s == "healthy" for s in states.values())
    finally:
        conn.close()


def test_retry_waiting_takes_priority(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed(conn, status="pending", task_id="P1", segment="A")
        _seed(conn, status="retry_waiting", task_id="R1", creative="cre2", segment="A",
              retry_count=1)
        claim = claim_one(conn)
        assert claim is not None
        assert claim.task_row["task_id"] == "R1"
    finally:
        conn.close()


def test_dependency_blocks_claim(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed(conn, task_id="A", segment="A")
        _seed(conn, task_id="B", creative="cre", segment="B", depends_on="A")
        # Drain A
        first = claim_one(conn)
        assert first is not None and first.task_row["task_id"] == "A"
        # B is depending on A which is running, not success: should not be claimed
        second = claim_one(conn)
        assert second is None
    finally:
        conn.close()


def test_dependency_allows_after_success(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed(conn, task_id="A", segment="A")
        _seed(conn, task_id="B", creative="cre", segment="B", depends_on="A")
        first = claim_one(conn)
        assert first is not None
        conn.execute("UPDATE tasks SET status='success' WHERE task_id='A'")
        # Free up the workstation so B can be claimed
        conn.execute(
            "UPDATE workstations SET status='healthy' WHERE id = ?",
            (first.workstation_id,),
        )
        second = claim_one(conn)
        assert second is not None and second.task_row["task_id"] == "B"
    finally:
        conn.close()


def test_max_retry_blocks_claim(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed(conn, status="retry_waiting", retry_count=2, max_retry=2)
        claim = claim_one(conn)
        assert claim is None
    finally:
        conn.close()


def test_concurrent_claim_only_one_wins(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        _seed(conn, task_id="ONLY")
    finally:
        conn.close()

    results: list[bool] = []
    barrier = threading.Barrier(2)

    def worker():
        c = connect(db_path)
        try:
            barrier.wait()
            res = claim_one(c)
            results.append(res is not None)
        finally:
            c.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert results.count(True) == 1
    assert results.count(False) == 1


def test_cross_day_reset_inside_claim(db_path: Path, workstations) -> None:
    sync_workstations(db_path, workstations)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    conn = connect(db_path)
    try:
        # Pretend WS_A's stats are stuck at yesterday & maxed out.
        conn.execute(
            "UPDATE workstations SET stats_date = ?, today_success_count = 9999, "
            "today_failure_count = 0 WHERE id = ?",
            (yesterday, "WS_A"),
        )
        _seed(conn)
        claim = claim_one(conn)
        # Should succeed because cross-day reset cleared the counter for someone.
        assert claim is not None
        # WS_A's counters should now reflect today.
        row = conn.execute(
            "SELECT stats_date, today_success_count FROM workstations WHERE id = 'WS_A'"
        ).fetchone()
        assert row["stats_date"] == date.today().isoformat()
    finally:
        conn.close()

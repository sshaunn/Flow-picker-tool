"""Background scheduler daemon."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.scheduler.daemon import SchedulerDaemon
from app.tasks.importer import import_tasks
from app.worker.flow_mock import MockRoundPlan


def _wait_until(predicate, timeout: float = 5.0, poll: float = 0.05) -> bool:
    """Poll ``predicate`` until True or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return predicate()


HEADER = (
    "task_id,sku_id,creative_id,segment_id,sequence_index,"
    "source_asset_path,source_asset_type,video_prompt,target_count,"
    "depends_on_task_id,max_retry_count\n"
)


def _write_csv(tmp_path: Path, n: int = 3) -> Path:
    img = tmp_path / "img.png"
    img.write_bytes(b"\x89PNG")
    rows = [HEADER.rstrip()]
    for i in range(n):
        rows.append(
            f"T{i+1:03d},sku,cre_{(i // 3) + 1:03d},{chr(ord('A') + (i % 3))},"
            f"{(i % 3) + 1},{img},first_frame,p,2,,"
        )
    p = tmp_path / "tasks.csv"
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return p


def _make_daemon(*, db_path, app_config, workstations, plans=None,
                 idle_poll_sec: float = 0.05) -> SchedulerDaemon:
    if plans is None:
        plans = {ws.id: [MockRoundPlan.success(2)] * 5 for ws in workstations}
    return SchedulerDaemon(
        db_path=db_path,
        config=app_config,
        workstations=workstations,
        idle_poll_sec=idle_poll_sec,
        use_mock=True,
        mock_round_plans_per_ws=plans,
    )


def test_daemon_drains_existing_tasks(
    tmp_path: Path, db_path: Path, app_config, workstations
) -> None:
    csv_path = _write_csv(tmp_path, n=3)
    import_tasks(csv_path, db_path, default_max_retry=2)

    daemon = _make_daemon(
        db_path=db_path, app_config=app_config, workstations=workstations,
    )
    daemon.start()
    try:
        ok = _wait_until(
            lambda: daemon.status().cumulative.success >= 3, timeout=5.0
        )
    finally:
        daemon.stop(timeout=5.0)

    assert ok, f"only {daemon.status().cumulative.success} succeeded in time"
    assert daemon.is_running is False


def test_daemon_picks_up_task_added_after_start(
    tmp_path: Path, db_path: Path, app_config, workstations
) -> None:
    """The whole point of running in a thread: tasks added later get picked up."""
    daemon = _make_daemon(
        db_path=db_path, app_config=app_config, workstations=workstations,
    )
    daemon.start()
    try:
        # Daemon starts on an empty queue and should idle-poll.
        time.sleep(0.2)
        assert daemon.status().cumulative.executed == 0

        # Now drop a task in.
        from app.db.connection import connect
        from app.tasks.repository import AssetDraft, TaskDraft, create_task

        img = tmp_path / "img.png"
        img.write_bytes(b"\x89PNG")
        with connect(db_path) as conn:
            create_task(conn, TaskDraft(
                sku_id="sku", creative_id="cre", segment_id="A",
                video_prompt="hello", target_count=2,
                assets=[AssetDraft(path=img, copy_into_managed_dir=False)],
            ))

        ok = _wait_until(
            lambda: daemon.status().cumulative.success >= 1, timeout=5.0
        )
    finally:
        daemon.stop(timeout=5.0)

    assert ok, "newly-added task was never executed by the daemon"


def test_daemon_start_is_idempotent(
    db_path: Path, app_config, workstations
) -> None:
    daemon = _make_daemon(
        db_path=db_path, app_config=app_config, workstations=workstations,
    )
    daemon.start()
    initial_thread = daemon._thread
    try:
        daemon.start()  # second call is a no-op
        assert daemon._thread is initial_thread
    finally:
        daemon.stop(timeout=5.0)


def test_daemon_stop_when_not_running_returns_true(
    db_path: Path, app_config, workstations
) -> None:
    daemon = _make_daemon(
        db_path=db_path, app_config=app_config, workstations=workstations,
    )
    assert daemon.stop(timeout=1.0) is True


def test_daemon_status_snapshot_is_independent_copy(
    tmp_path: Path, db_path: Path, app_config, workstations
) -> None:
    csv_path = _write_csv(tmp_path, n=3)
    import_tasks(csv_path, db_path, default_max_retry=2)

    daemon = _make_daemon(
        db_path=db_path, app_config=app_config, workstations=workstations,
    )
    daemon.start()
    try:
        _wait_until(
            lambda: daemon.status().cumulative.success >= 3, timeout=5.0
        )
        snap = daemon.status()
        success_at_snap = snap.cumulative.success
        # Mutate the snapshot — internal status must not change.
        snap.cumulative.success = -1
        snap.last_error = "external mutation"
    finally:
        daemon.stop(timeout=5.0)

    fresh = daemon.status()
    assert fresh.cumulative.success == success_at_snap
    assert fresh.last_error is None


def test_daemon_records_started_and_stopped_timestamps(
    db_path: Path, app_config, workstations
) -> None:
    daemon = _make_daemon(
        db_path=db_path, app_config=app_config, workstations=workstations,
    )
    daemon.start()
    started = daemon.status().started_at
    assert started is not None
    daemon.stop(timeout=5.0)
    stopped = daemon.status().stopped_at
    assert stopped is not None
    assert stopped >= started  # ISO UTC string comparison is order-preserving


def test_daemon_status_cumulates_across_passes(
    tmp_path: Path, db_path: Path, app_config, workstations
) -> None:
    """Each empty-queue idle bumps rounds_completed; cumulative sums add up."""
    csv_path = _write_csv(tmp_path, n=3)
    import_tasks(csv_path, db_path, default_max_retry=2)

    daemon = _make_daemon(
        db_path=db_path, app_config=app_config, workstations=workstations,
    )
    daemon.start()
    try:
        ok = _wait_until(
            lambda: daemon.status().rounds_completed >= 1, timeout=5.0
        )
    finally:
        daemon.stop(timeout=5.0)
    assert ok
    final = daemon.status()
    assert final.cumulative.executed == final.cumulative.success
    assert final.last_round_at is not None

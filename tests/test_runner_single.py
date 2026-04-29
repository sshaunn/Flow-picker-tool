"""T11 — single workstation runner end-to-end."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.db.connection import connect
from app.runner.single import run_single_workstation
from app.tasks.importer import import_tasks
from app.worker.flow_mock import MockRoundPlan
from app.worker.flow_port import PageState


HEADER = [
    "task_id", "sku_id", "creative_id", "segment_id", "sequence_index",
    "source_asset_path", "source_asset_type", "video_prompt", "target_count",
    "depends_on_task_id", "max_retry_count",
]


def _make_csv(tmp_path: Path, n: int = 3) -> Path:
    img = tmp_path / "img.png"
    img.write_bytes(b"\x89PNG")
    rows = [",".join(HEADER)]
    for i in range(n):
        rows.append(",".join([
            f"T{i+1}", "sku", "cre", chr(ord("A") + i), str(i + 1),
            str(img), "first_frame", "p", "4", "", "",
        ]))
    p = tmp_path / "tasks.csv"
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return p


def test_runner_executes_three_tasks_success(tmp_path: Path, db_path: Path,
                                              app_config, workstations) -> None:
    csv_path = _make_csv(tmp_path, n=3)
    import_tasks(csv_path, db_path, default_max_retry=2)

    plans = [MockRoundPlan.success(4)]  # one round of 4 candidates per task
    summary = run_single_workstation(
        db_path=db_path,
        config=app_config,
        workstations=workstations,
        target_workstation_id="WS_A",
        max_tasks=3,
        use_mock=True,
        mock_round_plans=plans * 3,  # MockFlowPort iterator across all 3 task runs
        today=date(2026, 4, 28),
    )
    assert summary.success == 3
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT status, downloaded_count FROM tasks ORDER BY task_id").fetchall()
        assert {r["status"] for r in rows} == {"success"}
        assert all(r["downloaded_count"] == 4 for r in rows)
    finally:
        conn.close()


def test_runner_login_required_stops_after_first_task(tmp_path: Path, db_path: Path,
                                                       app_config, workstations) -> None:
    csv_path = _make_csv(tmp_path, n=3)
    import_tasks(csv_path, db_path, default_max_retry=2)
    summary = run_single_workstation(
        db_path=db_path,
        config=app_config,
        workstations=workstations,
        target_workstation_id="WS_A",
        max_tasks=3,
        use_mock=True,
        mock_initial_state=PageState.LOGIN_REQUIRED,
        today=date(2026, 4, 28),
    )
    assert summary.executed == 1
    assert summary.retry_waiting == 1

    conn = connect(db_path)
    try:
        # First task should be retry_waiting with retry_count=1
        first = conn.execute(
            "SELECT task_id, status, retry_count FROM tasks "
            "WHERE status='retry_waiting' LIMIT 1"
        ).fetchone()
        assert first is not None
        assert first["retry_count"] == 1
        # Workstation should be in manual_check
        ws = conn.execute("SELECT status FROM workstations WHERE id='WS_A'").fetchone()
        assert ws["status"] == "manual_check"
        # Other 2 still pending
        n_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status='pending'"
        ).fetchone()["n"]
        assert n_pending == 2
    finally:
        conn.close()

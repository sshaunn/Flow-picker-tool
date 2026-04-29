"""T15 / T18 — multi-workstation runner integration."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.db.connection import connect
from app.runner.multi import run_multi_workstation
from app.tasks.importer import import_tasks
from app.worker.flow_mock import MockRoundPlan
from app.worker.flow_port import PageState


HEADER = [
    "task_id", "sku_id", "creative_id", "segment_id", "sequence_index",
    "source_asset_path", "source_asset_type", "video_prompt", "target_count",
    "depends_on_task_id", "max_retry_count",
]


def _make_csv(tmp_path: Path, n: int = 9) -> Path:
    img = tmp_path / "img.png"
    img.write_bytes(b"\x89PNG")
    rows = [",".join(HEADER)]
    for i in range(n):
        creative = f"cre_{(i // 3) + 1:03d}"
        segment = chr(ord("A") + (i % 3))
        rows.append(",".join([
            f"T{i+1:03d}", "sku", creative, segment, str((i % 3) + 1),
            str(img), "first_frame", "p", "4", "", "",
        ]))
    p = tmp_path / "tasks.csv"
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return p


def test_three_workstations_nine_tasks_no_double_claim(tmp_path: Path, db_path: Path,
                                                         app_config, workstations) -> None:
    _make_csv_path = _make_csv(tmp_path, n=9)
    import_tasks(_make_csv_path, db_path, default_max_retry=2)
    plans_per_ws = {
        ws.id: [MockRoundPlan.success(4) for _ in range(9)]
        for ws in workstations
    }
    summary = run_multi_workstation(
        db_path=db_path,
        config=app_config,
        workstations=workstations,
        max_rounds=0,
        use_mock=True,
        mock_round_plans_per_ws=plans_per_ws,
        today=date(2026, 4, 28),
    )
    assert summary.executed == 9
    assert summary.success == 9
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT task_id, status, assigned_workstation_id FROM tasks").fetchall()
        assert len(rows) == 9
        assert all(r["status"] == "success" for r in rows)
        # Every task got assigned exactly once.
        assert all(r["assigned_workstation_id"] is not None for r in rows)
        n_results = conn.execute("SELECT COUNT(*) AS n FROM task_results").fetchone()["n"]
        assert n_results == 9 * 4
    finally:
        conn.close()


def test_one_ws_manual_check_others_continue(tmp_path: Path, db_path: Path,
                                               app_config, workstations) -> None:
    csv_path = _make_csv(tmp_path, n=6)
    import_tasks(csv_path, db_path, default_max_retry=2)

    # WS_B will fail with login_required (no rounds will run); WS_A and WS_C succeed.
    plans_per_ws = {
        "WS_A": [MockRoundPlan.success(4)] * 6,
        "WS_B": [],  # will short-circuit at open() because mock_initial_state stays READY by default
        "WS_C": [MockRoundPlan.success(4)] * 6,
    }
    # We need a per-ws initial_state. The current API uses a single mock_initial_state,
    # so simulate WS_B failure differently: feed it a page error round.
    plans_per_ws["WS_B"] = [
        MockRoundPlan.page_error(PageState.LOGIN_REQUIRED, "login_required") for _ in range(6)
    ]

    summary = run_multi_workstation(
        db_path=db_path,
        config=app_config,
        workstations=workstations,
        max_rounds=0,
        use_mock=True,
        mock_round_plans_per_ws=plans_per_ws,
        today=date(2026, 4, 28),
    )
    conn = connect(db_path)
    try:
        ws_states = {
            r["id"]: r["status"]
            for r in conn.execute("SELECT id, status FROM workstations").fetchall()
        }
        # WS_B should be parked.
        assert ws_states.get("WS_B") == "manual_check"
        # All tasks reach a terminal-ish state.
        non_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status != 'pending'"
        ).fetchone()["n"]
        assert non_pending == 6
        # At least some succeeded via WS_A or WS_C.
        n_success = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status='success'"
        ).fetchone()["n"]
        assert n_success >= 1
        assert summary.executed >= 1
    finally:
        conn.close()


def test_workstation_daily_limit_skips_full_ws(tmp_path: Path, db_path: Path,
                                                 app_config, workstations) -> None:
    csv_path = _make_csv(tmp_path, n=3)
    import_tasks(csv_path, db_path, default_max_retry=2)

    # Lower WS_A's daily_task_limit to 1 in the configured fixture so sync
    # doesn't restore it to 20.
    for ws in workstations:
        if ws.id == "WS_A":
            ws.daily_task_limit = 1
    # Sync up-front so the row exists, then mark WS_A as already at limit today.
    from app.workstations.sync import sync_workstations
    from datetime import date as _date
    sync_workstations(db_path, workstations)
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE workstations SET stats_date=?, today_success_count=1 "
            "WHERE id='WS_A'",
            (_date.today().isoformat(),),
        )
    finally:
        conn.close()

    plans_per_ws = {
        ws.id: [MockRoundPlan.success(4) for _ in range(3)]
        for ws in workstations
    }
    run_multi_workstation(
        db_path=db_path,
        config=app_config,
        workstations=workstations,
        max_rounds=0,
        use_mock=True,
        mock_round_plans_per_ws=plans_per_ws,
        today=None,
    )
    conn = connect(db_path)
    try:
        # WS_A should not have picked up any of the new tasks.
        n_for_a = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE assigned_workstation_id='WS_A'"
        ).fetchone()["n"]
        assert n_for_a == 0
    finally:
        conn.close()

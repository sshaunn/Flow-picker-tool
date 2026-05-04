"""T08-T10 — candidate generation loop tests."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from app.db.connection import connect
from app.worker.flow_mock import MockFlowPort, MockRoundPlan
from app.worker.flow_port import PageState
from app.worker.loop import TaskInput, execute_task


def _seed_task(db_path: Path, target: int = 8) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
            "source_asset_path, source_asset_type, video_prompt, target_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("T1", "sku", "cre", "A", "/x.png", "first_frame", "p", target),
        )
    finally:
        conn.close()


def _task() -> TaskInput:
    return TaskInput(
        task_id="T1",
        sku_id="sku",
        creative_id="cre",
        segment_id="A",
        source_asset_path=Path("/x.png"),
        video_prompt="p",
        target_count=8,
    )


def test_two_rounds_each_4_candidates_succeeds(db_path: Path, app_config) -> None:
    _seed_task(db_path, target=8)
    flow = MockFlowPort([MockRoundPlan.success(4), MockRoundPlan.success(4)])
    log = logging.getLogger("test")
    conn = connect(db_path)
    try:
        outcome = execute_task(
            conn=conn,
            log=log,
            flow=flow,
            workstation_id="WS_A",
            task=_task(),
            config=app_config.generation,
            output_root=Path(app_config.output_root),
            run_date=date(2026, 4, 28),
        )
    finally:
        conn.close()
    assert outcome.final_status == "success"
    assert outcome.downloaded_count == 8
    assert outcome.generation_round_count == 2
    assert outcome.workstation_outcome == "healthy"
    assert len(outcome.candidates_persisted) == 8
    # Files actually exist on disk
    for c in outcome.candidates_persisted:
        assert Path(c["video_file_path"]).exists()


def test_partial_round_writes_error_log(db_path: Path, app_config) -> None:
    _seed_task(db_path, target=8)
    flow = MockFlowPort([MockRoundPlan.partial_download(4, 2)])
    log = logging.getLogger("test")
    conn = connect(db_path)
    try:
        outcome = execute_task(
            conn=conn,
            log=log,
            flow=flow,
            workstation_id="WS_A",
            task=_task(),
            config=app_config.generation,
            output_root=Path(app_config.output_root),
            run_date=date(2026, 4, 28),
        )
        # 2 of 4 download failures recorded as error_logs entries
        n_errors = conn.execute(
            "SELECT COUNT(*) AS n FROM error_logs WHERE task_id='T1' AND error_type='download_failed'"
        ).fetchone()["n"]
        assert n_errors == 2
        n_ok = conn.execute(
            "SELECT COUNT(*) AS n FROM task_results WHERE task_id='T1'"
        ).fetchone()["n"]
        assert n_ok == 2
    finally:
        conn.close()
    assert outcome.downloaded_count == 2


def test_all_downloads_fail_marks_download_failed(db_path: Path, app_config) -> None:
    _seed_task(db_path, target=8)
    flow = MockFlowPort([MockRoundPlan.all_downloads_fail(3)])
    log = logging.getLogger("test")
    conn = connect(db_path)
    try:
        outcome = execute_task(
            conn=conn,
            log=log,
            flow=flow,
            workstation_id="WS_A",
            task=_task(),
            config=app_config.generation,
            output_root=Path(app_config.output_root),
            run_date=date(2026, 4, 28),
        )
    finally:
        conn.close()
    assert outcome.final_status == "download_failed"
    assert outcome.downloaded_count == 0
    assert outcome.workstation_outcome == "healthy"  # download issue, not page issue


def test_one_round_then_all_download_fail_persists_partial(db_path: Path, app_config) -> None:
    _seed_task(db_path, target=8)
    flow = MockFlowPort(
        [MockRoundPlan.success(4), MockRoundPlan.all_downloads_fail(3)]
    )
    log = logging.getLogger("test")
    conn = connect(db_path)
    try:
        outcome = execute_task(
            conn=conn,
            log=log,
            flow=flow,
            workstation_id="WS_A",
            task=_task(),
            config=app_config.generation,
            output_root=Path(app_config.output_root),
            run_date=date(2026, 4, 28),
        )
    finally:
        conn.close()
    assert outcome.final_status == "download_failed"
    assert outcome.downloaded_count == 4  # first round preserved


def test_unusual_activity_breaks_to_retry_waiting(db_path: Path, app_config) -> None:
    _seed_task(db_path, target=8)
    flow = MockFlowPort(
        [MockRoundPlan.page_error(PageState.UNUSUAL_ACTIVITY, "blocked")]
    )
    log = logging.getLogger("test")
    conn = connect(db_path)
    try:
        outcome = execute_task(
            conn=conn,
            log=log,
            flow=flow,
            workstation_id="WS_A",
            task=_task(),
            config=app_config.generation,
            output_root=Path(app_config.output_root),
            run_date=date(2026, 4, 28),
        )
    finally:
        conn.close()
    assert outcome.final_status == "retry_waiting"
    assert outcome.workstation_outcome == "manual_check"
    assert outcome.last_error_type == "unusual_activity"


def test_resumed_task_gets_full_max_round_budget(db_path: Path, app_config) -> None:
    """Regression: a task resumed with ``initial_round_count`` already
    at the max_round_per_task cap should still get a fresh window of
    rounds. Earlier the worker tripped ``round_count >= max_round``
    on iteration 0, exited in 5s, and re-marked the task failed without
    generating anything (the customer's "继续任务 没用" symptom).

    Storage cursor still advances forward (round 21, 22 here) so we
    don't collide with old ``task_results`` rows from the prior session.
    """
    cfg = app_config.generation
    cfg.max_round_per_task = 2
    _seed_task(db_path, target=8)
    flow = MockFlowPort([MockRoundPlan.success(2), MockRoundPlan.success(2)])
    log = logging.getLogger("test")
    resumed_task = TaskInput(
        task_id="T1", sku_id="sku", creative_id="cre", segment_id="A",
        source_asset_path=Path("/x.png"), video_prompt="p",
        target_count=8,
        initial_downloaded_count=4,  # carried over from prior session
        initial_round_count=20,      # prior session exhausted max_round
    )
    conn = connect(db_path)
    try:
        outcome = execute_task(
            conn=conn, log=log, flow=flow, workstation_id="WS_A",
            task=resumed_task, config=cfg,
            output_root=Path(app_config.output_root),
            run_date=date(2026, 4, 28),
        )
    finally:
        conn.close()
    # Both rounds ran in the resumed session — 4 new downloads on top
    # of the carried-over 4 = 8/8 success.
    assert outcome.final_status == "success"
    assert outcome.downloaded_count == 8
    # Storage cursor advanced past the 20 already used.
    assert outcome.generation_round_count == 22


def test_max_round_with_partial_marks_failed(db_path: Path, app_config) -> None:
    """target=8, max_round=2, 2 rounds × 2 downloads = 4/8 -> failed."""
    cfg = app_config.generation
    cfg.max_round_per_task = 2
    _seed_task(db_path, target=8)
    flow = MockFlowPort([MockRoundPlan.success(2), MockRoundPlan.success(2)])
    log = logging.getLogger("test")
    conn = connect(db_path)
    try:
        outcome = execute_task(
            conn=conn,
            log=log,
            flow=flow,
            workstation_id="WS_A",
            task=_task(),
            config=cfg,
            output_root=Path(app_config.output_root),
            run_date=date(2026, 4, 28),
        )
    finally:
        conn.close()
    assert outcome.final_status == "failed"
    assert outcome.downloaded_count == 4


def test_service_unavailable_during_round_treated_as_page_failure(
    db_path: Path, app_config
) -> None:
    """Flow 'high demand' should not flip the workstation to manual_check —
    it's a transient service-level error and should let cooldown handle it."""
    _seed_task(db_path, target=8)
    flow = MockFlowPort(
        [MockRoundPlan.page_error(PageState.SERVICE_UNAVAILABLE,
                                    "Flow is experiencing high demand")]
    )
    log = logging.getLogger("test")
    conn = connect(db_path)
    try:
        outcome = execute_task(
            conn=conn,
            log=log,
            flow=flow,
            workstation_id="WS_A",
            task=_task(),
            config=app_config.generation,
            output_root=Path(app_config.output_root),
            run_date=date(2026, 4, 28),
        )
    finally:
        conn.close()
    assert outcome.final_status == "retry_waiting"
    assert outcome.workstation_outcome == "page_failure"  # NOT manual_check
    assert outcome.last_error_type == "service_unavailable"


def test_worker_uploads_multiple_assets_in_order(db_path: Path, app_config, tmp_path) -> None:
    """The worker should hand the ordered asset list to the FlowPort —
    not just the legacy single source_asset_path."""
    from app.worker.flow_port import SourceAsset

    _seed_task(db_path, target=4)
    flow = MockFlowPort([MockRoundPlan.success(4)])
    log = logging.getLogger("test")
    a = tmp_path / "first.png"; a.write_bytes(b"\x89PNG-A")
    b = tmp_path / "last.png";  b.write_bytes(b"\x89PNG-B")
    task = TaskInput(
        task_id="T1", sku_id="sku", creative_id="cre", segment_id="A",
        source_asset_path=a, video_prompt="p", target_count=4,
        source_assets=[
            SourceAsset(path=a, kind="first_frame", order=1),
            SourceAsset(path=b, kind="last_frame", order=2),
        ],
    )
    conn = connect(db_path)
    try:
        execute_task(
            conn=conn, log=log, flow=flow, workstation_id="WS_A",
            task=task, config=app_config.generation,
            output_root=Path(app_config.output_root),
            run_date=date(2026, 4, 28),
        )
    finally:
        conn.close()
    assert len(flow.upload_calls) == 1, "single round = single upload call"
    uploaded = flow.upload_calls[0]
    assert [a.kind for a in uploaded] == ["first_frame", "last_frame"]
    assert [a.order for a in uploaded] == [1, 2]


def test_worker_falls_back_to_legacy_single_asset(db_path: Path, app_config, tmp_path) -> None:
    """Tasks with empty source_assets should still upload the single
    legacy ``source_asset_path`` so old import paths keep working."""
    _seed_task(db_path, target=4)
    flow = MockFlowPort([MockRoundPlan.success(4)])
    log = logging.getLogger("test")
    legacy = tmp_path / "legacy.png"; legacy.write_bytes(b"\x89PNG")
    task = TaskInput(
        task_id="T1", sku_id="sku", creative_id="cre", segment_id="A",
        source_asset_path=legacy, video_prompt="p", target_count=4,
        # source_assets=[]  ← empty, runner-level fallback exercised here
    )
    conn = connect(db_path)
    try:
        execute_task(
            conn=conn, log=log, flow=flow, workstation_id="WS_A",
            task=task, config=app_config.generation,
            output_root=Path(app_config.output_root),
            run_date=date(2026, 4, 28),
        )
    finally:
        conn.close()
    uploaded = flow.upload_calls[0]
    assert len(uploaded) == 1
    assert uploaded[0].path == legacy
    assert uploaded[0].kind == "first_frame"


def test_login_required_on_open(db_path: Path, app_config) -> None:
    _seed_task(db_path, target=8)
    flow = MockFlowPort([], initial_state=PageState.LOGIN_REQUIRED)
    log = logging.getLogger("test")
    conn = connect(db_path)
    try:
        outcome = execute_task(
            conn=conn,
            log=log,
            flow=flow,
            workstation_id="WS_A",
            task=_task(),
            config=app_config.generation,
            output_root=Path(app_config.output_root),
            run_date=date(2026, 4, 28),
        )
    finally:
        conn.close()
    assert outcome.final_status == "retry_waiting"
    assert outcome.workstation_outcome == "manual_check"
    assert outcome.last_error_type == "login_required"
    assert outcome.generation_round_count == 0

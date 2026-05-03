"""Day / night operation-mode persistence + per-mode behavior."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import paths as app_paths
from app.config.loader import ModeProfile, OperationModeSettings
from app.db.connection import connect
from app.state import OperationMode, get_operation_mode, set_operation_mode
from app.web.server import create_app


# ----------------------------------------------------- repository


def test_default_mode_is_day_for_fresh_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        assert get_operation_mode(conn) == OperationMode.DAY


def test_set_and_get_mode_round_trip(db_path: Path) -> None:
    with connect(db_path) as conn:
        set_operation_mode(conn, OperationMode.NIGHT)
        assert get_operation_mode(conn) == OperationMode.NIGHT
        set_operation_mode(conn, "day")
        assert get_operation_mode(conn) == OperationMode.DAY


def test_corrupted_value_falls_back_to_day(db_path: Path) -> None:
    """Manual DB edit that wrote something invalid shouldn't crash the
    daemon — fall back to the safe default."""
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES ('operation_mode', 'gibberish')"
        )
        conn.commit()
        assert get_operation_mode(conn) == OperationMode.DAY


def test_set_invalid_string_raises(db_path: Path) -> None:
    with connect(db_path) as conn:
        with pytest.raises(ValueError):
            set_operation_mode(conn, "twilight")


# ----------------------------------------------------- config defaults


def test_mode_profiles_have_distinct_safe_defaults() -> None:
    s = OperationModeSettings()
    assert s.day.stagger_sec < s.night.stagger_sec
    assert s.day.max_concurrent_ws > s.night.max_concurrent_ws
    assert s.day.captcha_action == "pause"
    assert s.night.captcha_action == "skip"
    assert s.day.auto_resume_cap <= s.night.auto_resume_cap


def test_mode_profile_rejects_unknown_captcha_action() -> None:
    with pytest.raises(ValueError):
        ModeProfile(captcha_action="ignore")


# ----------------------------------------------------- HTTP API


@pytest.fixture(autouse=True)
def _isolate_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(app_paths._ENV_DATA_DIR, str(tmp_path / "data"))


def _client(app_config) -> TestClient:
    app = create_app(
        config=app_config, auto_start_daemon=False,
        idle_poll_sec=0.05, push_interval_sec=10.0,
        use_mock=True,
    )
    return TestClient(app)


def test_api_get_returns_default_day(app_config) -> None:
    with _client(app_config) as client:
        resp = client.get("/api/mode")
    assert resp.status_code == 200
    assert resp.json() == {"value": "day"}


def test_api_post_persists_and_round_trips(app_config) -> None:
    with _client(app_config) as client:
        resp = client.post("/api/mode", json={"value": "night"})
        assert resp.status_code == 200
        assert resp.json() == {"value": "night"}
        # And a fresh GET sees the new value.
        assert client.get("/api/mode").json() == {"value": "night"}


def test_api_post_rejects_unknown_value(app_config) -> None:
    with _client(app_config) as client:
        resp = client.post("/api/mode", json={"value": "twilight"})
    assert resp.status_code == 422


# ----------------------------------------------------- UI


def test_top_nav_paints_current_mode_active(app_config) -> None:
    """The toggle button should carry the current mode as a data attr so
    the active class is correct on first paint, no JS flash."""
    with _client(app_config) as client:
        resp = client.get("/")
        assert 'id="mode-toggle"' in resp.text
        assert 'data-current="day"' in resp.text

        # Flip to night and re-render — the data attr follows.
        client.post("/api/mode", json={"value": "night"})
        resp = client.get("/")
    assert 'data-current="night"' in resp.text


# ----------------------------------------------------- worker captcha branch


def test_worker_captcha_skip_marks_manual_review() -> None:
    """Night mode (captcha_action='skip'): a captcha-blocked task ends
    up in manual_review instead of cycling through retry_waiting."""
    from app.worker.flow_mock import MockFlowPort, MockRoundPlan
    from app.worker.flow_port import PageState
    from app.worker.loop import TaskInput, execute_task
    from app.config.loader import GenerationSettings
    import logging
    import sqlite3
    import tempfile

    db_file = tempfile.mktemp(suffix=".sqlite")
    from app.db.schema import init_schema
    init_schema(db_file)
    conn = connect(db_file)
    conn.execute(
        "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
        "sequence_index, source_asset_path, source_asset_type, "
        "video_prompt, target_count) VALUES "
        "('T_SKIP', 's', 'c', 'A', 1, '/x', 'first_frame', 'p', 4)"
    )
    conn.commit()

    flow = MockFlowPort(
        round_plans=[MockRoundPlan.page_error(
            PageState.CAPTCHA_OR_VERIFICATION, "captcha_or_verification",
        )],
        initial_state=PageState.READY,
    )
    task = TaskInput(
        task_id="T_SKIP", sku_id="s", creative_id="c", segment_id="A",
        source_asset_path=Path("/x"), video_prompt="p", target_count=4,
    )
    out_root = Path(tempfile.mkdtemp(prefix="flow_test_"))
    outcome = execute_task(
        conn=conn, log=logging.getLogger("test"), flow=flow,
        workstation_id="WS_X", task=task,
        config=GenerationSettings(),
        output_root=out_root,
        run_date=__import__("datetime").date(2026, 5, 3),
        captcha_action="skip",
    )
    assert outcome.final_status == "manual_review"
    assert outcome.last_error_type == "captcha_or_verification"


def test_worker_captcha_pause_keeps_retry_waiting() -> None:
    """Day mode (captcha_action='pause'): keep the task in retry_waiting
    so it tries again after the operator clears the captcha."""
    from app.worker.flow_mock import MockFlowPort, MockRoundPlan
    from app.worker.flow_port import PageState
    from app.worker.loop import TaskInput, execute_task
    from app.config.loader import GenerationSettings
    import logging
    import sqlite3
    import tempfile

    db_file = tempfile.mktemp(suffix=".sqlite")
    from app.db.schema import init_schema
    init_schema(db_file)
    conn = connect(db_file)
    conn.execute(
        "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
        "sequence_index, source_asset_path, source_asset_type, "
        "video_prompt, target_count) VALUES "
        "('T_PAUSE', 's', 'c', 'A', 1, '/x', 'first_frame', 'p', 4)"
    )
    conn.commit()

    flow = MockFlowPort(
        round_plans=[MockRoundPlan.page_error(
            PageState.CAPTCHA_OR_VERIFICATION, "captcha_or_verification",
        )],
        initial_state=PageState.READY,
    )
    task = TaskInput(
        task_id="T_PAUSE", sku_id="s", creative_id="c", segment_id="A",
        source_asset_path=Path("/x"), video_prompt="p", target_count=4,
    )
    out_root = Path(tempfile.mkdtemp(prefix="flow_test_"))
    outcome = execute_task(
        conn=conn, log=logging.getLogger("test"), flow=flow,
        workstation_id="WS_X", task=task,
        config=GenerationSettings(),
        output_root=out_root,
        run_date=__import__("datetime").date(2026, 5, 3),
        captcha_action="pause",
    )
    assert outcome.final_status == "retry_waiting"

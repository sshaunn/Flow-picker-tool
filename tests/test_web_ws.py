"""WebSocket dashboard push tests."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import paths as app_paths
from app.web.server import create_app


@pytest.fixture(autouse=True)
def _redirect_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(app_paths._ENV_DATA_DIR, str(tmp_path / "data"))


def _client(app_config) -> TestClient:
    app = create_app(
        config=app_config, auto_start_daemon=False,
        idle_poll_sec=0.05, push_interval_sec=0.05,
        use_mock=True,
    )
    return TestClient(app)


def test_ws_dashboard_pushes_initial_fragment(app_config) -> None:
    with _client(app_config) as client:
        with client.websocket_connect("/ws/dashboard") as ws:
            html = ws.receive_text()
    # The fragment carries the section headers from the partial.
    assert "Scheduler" in html
    assert "Workstations" in html
    assert "Recent tasks" in html


def test_ws_dashboard_reflects_db_state(app_config) -> None:
    with _client(app_config) as client:
        client.post("/api/workstations", json={
            "id": "WS_LIVE", "account_label": "live",
            "browser_profile_path": "/tmp/x", "daily_task_limit": 5,
        })
        files = [("assets", ("a.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"), "image/png"))]
        client.post("/api/tasks", files=files, data={
            "sku_id": "s", "creative_id": "c", "segment_id": "A",
            "video_prompt": "ws push smoke", "target_count": "2",
        })
        with client.websocket_connect("/ws/dashboard") as ws:
            html = ws.receive_text()
    assert "WS_LIVE" in html
    assert "ws push smoke" in html


def test_ws_dashboard_pushes_repeatedly(app_config) -> None:
    """Confirm the loop keeps sending; each tick should yield a payload."""
    with _client(app_config) as client:
        with client.websocket_connect("/ws/dashboard") as ws:
            first = ws.receive_text()
            second = ws.receive_text()
            third = ws.receive_text()
    assert first and second and third
    # Same payload across ticks when state hasn't changed — that's expected.
    assert all("Scheduler" in fragment for fragment in (first, second, third))


def test_ws_dashboard_picks_up_scheduler_state_change(app_config) -> None:
    with _client(app_config) as client:
        with client.websocket_connect("/ws/dashboard") as ws:
            before = ws.receive_text()
            assert "idle" in before  # daemon not running yet

            client.post("/api/scheduler/start")
            # Skip past any in-flight buffered tick, then check the next push.
            for _ in range(5):
                fragment = ws.receive_text()
                if "running" in fragment:
                    break
            else:
                pytest.fail("WebSocket never reflected the running daemon state")

            client.post("/api/scheduler/stop")

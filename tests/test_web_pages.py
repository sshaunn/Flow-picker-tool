"""Server-rendered HTML pages — Jinja2 + form posts."""

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
        idle_poll_sec=0.05, use_mock=True,
    )
    return TestClient(app)


# ---------------------------------------------------------------- dashboard


def test_dashboard_renders_when_empty(app_config) -> None:
    with _client(app_config) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "Flow Harvester" in resp.text
    # Customer-facing copy is Chinese; check for one of the section
    # headers that's always present.
    assert "调度器" in resp.text


def test_dashboard_shows_workstations_and_tasks(app_config) -> None:
    with _client(app_config) as client:
        client.post("/api/workstations", json={
            "id": "WS_DASH", "account_label": "acct",
            "browser_profile_path": "/tmp/x", "daily_task_limit": 5,
        })
        files = [("assets", ("a.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"), "image/png"))]
        client.post("/api/tasks", files=files, data={
            "sku_id": "s", "creative_id": "c", "segment_id": "A",
            "video_prompt": "smoke", "target_count": "2",
        })
        resp = client.get("/")
    assert resp.status_code == 200
    assert "WS_DASH" in resp.text
    assert "smoke" in resp.text


# --------------------------------------------------------------- workstations


def test_workstations_list_page_renders(app_config) -> None:
    with _client(app_config) as client:
        resp = client.get("/workstations")
    assert resp.status_code == 200
    assert "添加" in resp.text


def test_workstations_new_form_renders(app_config) -> None:
    with _client(app_config) as client:
        resp = client.get("/workstations/new")
    assert resp.status_code == 200
    assert 'name="account_label"' in resp.text
    # project URL + mode preset are captured by the login flow, not typed
    # into this form.
    assert 'name="flow_project_url"' not in resp.text


def test_workstation_form_post_creates_and_redirects(app_config) -> None:
    with _client(app_config) as client:
        resp = client.post(
            "/workstations/new",
            data={
                "id": "WS_FORM", "account_label": "acct",
                "daily_task_limit": "10",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/workstations/WS_FORM"

        detail = client.get("/workstations/WS_FORM")
    assert detail.status_code == 200
    assert "WS_FORM" in detail.text


def test_workstation_detail_404_for_missing(app_config) -> None:
    with _client(app_config) as client:
        assert client.get("/workstations/NOPE").status_code == 404


def test_workstation_edit_form_prefills(app_config) -> None:
    with _client(app_config) as client:
        client.post("/workstations/new", data={
            "id": "WS_EDIT", "account_label": "old",
            "daily_task_limit": "5", "browser_profile_path": "/tmp/p",
        }, follow_redirects=False)
        resp = client.get("/workstations/WS_EDIT/edit")
    assert resp.status_code == 200
    assert 'value="WS_EDIT"' in resp.text
    assert 'value="old"' in resp.text


def test_workstation_edit_post_updates(app_config) -> None:
    with _client(app_config) as client:
        client.post("/workstations/new", data={
            "id": "WS_UP", "account_label": "old",
            "daily_task_limit": "5", "browser_profile_path": "/tmp/p",
        }, follow_redirects=False)
        resp = client.post(
            "/workstations/WS_UP/edit",
            data={"account_label": "new", "daily_task_limit": "9"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        detail = client.get("/workstations/WS_UP")
    assert "new" in detail.text


# ----------------------------------------------------------------------- tasks


def test_tasks_list_page_renders_when_empty(app_config) -> None:
    with _client(app_config) as client:
        resp = client.get("/tasks")
    assert resp.status_code == 200
    assert "新建任务" in resp.text


def test_tasks_new_form_renders(app_config) -> None:
    with _client(app_config) as client:
        resp = client.get("/tasks/new")
    assert resp.status_code == 200
    assert 'name="video_prompt"' in resp.text
    assert 'enctype="multipart/form-data"' in resp.text


def test_task_form_post_creates_and_redirects(app_config) -> None:
    files = [("assets", ("a.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"), "image/png"))]
    data = {
        "sku_id": "sku", "creative_id": "cre", "segment_id": "A",
        "video_prompt": "form-post smoke", "target_count": "3",
    }
    with _client(app_config) as client:
        resp = client.post("/tasks/new", files=files, data=data, follow_redirects=False)
        assert resp.status_code == 303
        new_url = resp.headers["location"]
        assert new_url.startswith("/tasks/T_")

        detail = client.get(new_url)
    assert detail.status_code == 200
    assert "form-post smoke" in detail.text


def test_task_detail_404_for_missing(app_config) -> None:
    with _client(app_config) as client:
        assert client.get("/tasks/NOPE").status_code == 404


def test_tasks_status_filter_passes_through(app_config) -> None:
    with _client(app_config) as client:
        resp = client.get("/tasks?status=pending")
    assert resp.status_code == 200
    # Active filter pill shows the queried status.
    assert "pending" in resp.text

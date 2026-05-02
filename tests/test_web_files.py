"""File serving + open-folder + worker log tail."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

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
        idle_poll_sec=0.05, push_interval_sec=10.0,
        use_mock=True,
    )
    return TestClient(app)


def _make_task_with_result(client: TestClient, app_config, *, suffix: str) -> tuple[str, Path]:
    """Helper: create a task + insert a fake task_results row pointing at a
    real file under output_root so we can hit /files for it."""
    files = [("assets", ("a.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"), "image/png"))]
    data = {
        "sku_id": "sku", "creative_id": f"cre_{suffix}", "segment_id": "A",
        "video_prompt": "p", "target_count": "1",
    }
    resp = client.post("/api/tasks", files=files, data=data)
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task_id"]

    output_root = Path(app_config.output_root)
    seg_dir = output_root / "2026-05-02" / "sku" / f"cre_{suffix}" / "segment_A"
    seg_dir.mkdir(parents=True, exist_ok=True)
    video_path = seg_dir / f"{task_id}_round_01_seq_01.mp4"
    video_path.write_bytes(b"FAKEMP4")

    from app.db.connection import connect
    conn = connect(app_config.db_path)
    try:
        conn.execute(
            "INSERT INTO task_results "
            "(task_id, creative_id, segment_id, workstation_id, "
            " generation_round, sequence_no, video_file_path, status) "
            "VALUES (?, ?, 'A', NULL, 1, 1, ?, 'downloaded')",
            (task_id, f"cre_{suffix}", str(video_path)),
        )
        conn.commit()
    finally:
        conn.close()
    return task_id, video_path


# ---------------------------------------------------------------- /files


def test_files_serves_video_under_output_root(app_config) -> None:
    with _client(app_config) as client:
        _, video_path = _make_task_with_result(client, app_config, suffix="A")
        rel = video_path.resolve().relative_to(Path(app_config.output_root).resolve())
        resp = client.get(f"/files/{rel}")
    assert resp.status_code == 200
    assert resp.content == b"FAKEMP4"


def test_files_rejects_traversal(app_config) -> None:
    with _client(app_config) as client:
        # The path-traversal guard should refuse before any FS lookup.
        resp = client.get("/files/../etc/passwd")
    assert resp.status_code in (400, 404)


def test_files_404_for_missing_file(app_config) -> None:
    with _client(app_config) as client:
        resp = client.get("/files/2026-05-02/nope/nope.mp4")
    assert resp.status_code == 404


# ----------------------------------------------------- /api/.../open-folder


def test_open_folder_invokes_os_command(app_config) -> None:
    with _client(app_config) as client:
        task_id, video_path = _make_task_with_result(client, app_config, suffix="B")
        with patch("subprocess.Popen") as popen, \
             patch("os.startfile", create=True) as startfile:
            resp = client.post(f"/api/tasks/{task_id}/open-folder")
        assert resp.status_code == 204
        # Either Popen (mac/linux) or startfile (win) was called with the
        # task's segment dir.
        called = popen.called or startfile.called
        assert called, "open-folder should invoke a shell-out"


def test_open_folder_404_for_unknown_task(app_config) -> None:
    with _client(app_config) as client:
        resp = client.post("/api/tasks/NOPE/open-folder")
    assert resp.status_code == 404


# ----------------------------------------------------- /tasks/.../log/partial


def test_log_partial_for_unassigned_task(app_config) -> None:
    files = [("assets", ("a.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"), "image/png"))]
    data = {"sku_id": "s", "creative_id": "c", "segment_id": "A",
            "video_prompt": "p", "target_count": "1"}
    with _client(app_config) as client:
        tid = client.post("/api/tasks", files=files, data=data).json()["task_id"]
        resp = client.get(f"/tasks/{tid}/log/partial")
    assert resp.status_code == 200
    assert "not yet assigned" in resp.text


def test_log_partial_filters_by_task_id(app_config, tmp_path: Path) -> None:
    """Worker log tail should only return lines mentioning this task id."""
    files = [("assets", ("a.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"), "image/png"))]
    data = {"sku_id": "s", "creative_id": "log_test", "segment_id": "A",
            "video_prompt": "p", "target_count": "1"}
    with _client(app_config) as client:
        tid = client.post("/api/tasks", files=files, data=data).json()["task_id"]

        from app.db.connection import connect
        conn = connect(app_config.db_path)
        try:
            conn.execute(
                "UPDATE tasks SET assigned_workstation_id = 'WS_LOG' WHERE task_id = ?",
                (tid,),
            )
            conn.commit()
        finally:
            conn.close()

        log_path = Path(app_config.log_root) / "worker_WS_LOG.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "[INFO] task_id=other-task irrelevant line\n"
            f"[INFO] task_id={tid} round 1 starting\n"
            "[INFO] task_id=other-task another irrelevant line\n"
            f"[INFO] task_id={tid} candidate 1/4 downloaded\n",
            encoding="utf-8",
        )
        resp = client.get(f"/tasks/{tid}/log/partial")
    assert resp.status_code == 200
    assert "round 1 starting" in resp.text
    assert "candidate 1/4 downloaded" in resp.text
    assert "irrelevant" not in resp.text


def test_log_partial_404_for_unknown_task(app_config) -> None:
    with _client(app_config) as client:
        resp = client.get("/tasks/NOPE/log/partial")
    assert resp.status_code == 404


# ----------------------------------------------------- /ws/task


def test_task_detail_ws_pushes_status_fragment(app_config) -> None:
    with _client(app_config) as client:
        task_id, _ = _make_task_with_result(client, app_config, suffix="ws")
        with client.websocket_connect(f"/ws/task/{task_id}") as ws:
            html = ws.receive_text()
    # WS pushes only the status partial — videos live in a separately
    # polled section so previewing a clip isn't interrupted by every tick.
    assert "Status" in html
    assert "<video" not in html


def test_videos_partial_renders_tiles(app_config) -> None:
    with _client(app_config) as client:
        task_id, _ = _make_task_with_result(client, app_config, suffix="vp")
        resp = client.get(f"/tasks/{task_id}/videos/partial")
    assert resp.status_code == 200
    assert "<video" in resp.text
    assert "data-video-tile" in resp.text


def test_task_detail_ws_closes_when_task_deleted(app_config) -> None:
    files = [("assets", ("a.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"), "image/png"))]
    data = {"sku_id": "s", "creative_id": "cls", "segment_id": "A",
            "video_prompt": "p", "target_count": "1"}
    with _client(app_config) as client:
        tid = client.post("/api/tasks", files=files, data=data).json()["task_id"]
        client.delete(f"/api/tasks/{tid}")
        with pytest.raises(Exception):
            with client.websocket_connect(f"/ws/task/{tid}") as ws:
                ws.receive_text()
                ws.receive_text()

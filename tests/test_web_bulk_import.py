"""Bulk CSV+images task import via the Web API."""

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
        idle_poll_sec=0.05, push_interval_sec=10.0,
        use_mock=True,
    )
    return TestClient(app)


_PNG = b"\x89PNG\r\n\x1a\n"


def test_bulk_import_creates_each_csv_row(app_config) -> None:
    csv_text = (
        "sku_id,creative_id,segment_id,video_prompt,target_count,source_asset_path\n"
        "sku_001,cre_A,A,prompt one,2,a.png\n"
        "sku_001,cre_A,B,prompt two,2,b.png\n"
        "sku_002,cre_B,A,prompt three,2,a.png\n"
    )
    with _client(app_config) as client:
        files = [
            ("csv_file", ("tasks.csv", io.BytesIO(csv_text.encode("utf-8")), "text/csv")),
            ("images", ("a.png", io.BytesIO(_PNG), "image/png")),
            ("images", ("b.png", io.BytesIO(_PNG), "image/png")),
        ]
        resp = client.post("/api/tasks/bulk-import", files=files)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["inserted"] == 3
    assert body["skipped"] == 0
    assert len(body["task_ids"]) == 3


def test_bulk_import_reports_invalid_rows_without_aborting(app_config) -> None:
    """One bad row shouldn't poison the whole import."""
    csv_text = (
        "sku_id,creative_id,segment_id,video_prompt,target_count,source_asset_path\n"
        "sku,cre,A,ok,2,present.png\n"
        "sku,cre,B,missing image,2,missing.png\n"
        "sku,cre,C,bad target,0,present.png\n"
    )
    with _client(app_config) as client:
        files = [
            ("csv_file", ("tasks.csv", io.BytesIO(csv_text.encode("utf-8")), "text/csv")),
            ("images", ("present.png", io.BytesIO(_PNG), "image/png")),
        ]
        resp = client.post("/api/tasks/bulk-import", files=files)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["inserted"] == 1
    assert body["skipped"] == 2
    assert any("missing.png" in e for e in body["errors"])
    assert any("第 4 行" in e for e in body["errors"])


def test_bulk_import_rejects_non_utf8_csv(app_config) -> None:
    """GBK-encoded CSV (common Windows export) should fail with a clear msg."""
    csv_text = "sku_id,creative_id,segment_id,video_prompt,target_count,source_asset_path\n中文,cre,A,p,2,a.png\n"
    with _client(app_config) as client:
        files = [
            ("csv_file", ("tasks.csv", io.BytesIO(csv_text.encode("gbk")), "text/csv")),
            ("images", ("a.png", io.BytesIO(_PNG), "image/png")),
        ]
        resp = client.post("/api/tasks/bulk-import", files=files)
    assert resp.status_code == 400
    assert "UTF-8" in resp.text


def test_bulk_import_rejects_empty_csv(app_config) -> None:
    csv_text = "sku_id,creative_id,segment_id,video_prompt,target_count,source_asset_path\n"
    with _client(app_config) as client:
        files = [
            ("csv_file", ("tasks.csv", io.BytesIO(csv_text.encode("utf-8")), "text/csv")),
            ("images", ("a.png", io.BytesIO(_PNG), "image/png")),
        ]
        resp = client.post("/api/tasks/bulk-import", files=files)
    assert resp.status_code == 400


def test_bulk_import_per_row_flow_mode_override(app_config) -> None:
    """Each CSV row can carry its own model / duration / aspect; rows
    without mode_* columns fall back to the workstation preset."""
    csv_text = (
        "sku_id,creative_id,segment_id,video_prompt,target_count,"
        "source_asset_path,mode_model,mode_duration_sec,mode_aspect\n"
        "s,fm,A,fast vertical,2,a.png,Veo 3.1 - Fast,4,9:16\n"
        "s,fm,B,quality square,2,a.png,Veo 3.1 - Quality,8,1:1\n"
        "s,fm,C,no override,2,a.png,,,\n"
    )
    with _client(app_config) as client:
        files = [
            ("csv_file", ("tasks.csv", io.BytesIO(csv_text.encode("utf-8")), "text/csv")),
            ("images", ("a.png", io.BytesIO(_PNG), "image/png")),
        ]
        resp = client.post("/api/tasks/bulk-import", files=files)
        assert resp.status_code == 201
        body = resp.json()
        assert body["inserted"] == 3

        # Check each task ended up with the right (or absent) flow_mode.
        details = [client.get(f"/api/tasks/{tid}").json() for tid in body["task_ids"]]
    by_segment = {d["segment_id"]: d for d in details}
    assert by_segment["A"]["flow_mode"]["model"] == "Veo 3.1 - Fast"
    assert by_segment["A"]["flow_mode"]["duration_sec"] == 4
    assert by_segment["A"]["flow_mode"]["aspect"] == "9:16"
    assert by_segment["B"]["flow_mode"]["model"] == "Veo 3.1 - Quality"
    assert by_segment["B"]["flow_mode"]["aspect"] == "1:1"
    # All-empty mode_* columns -> no override -> flow_mode stays None.
    assert by_segment["C"]["flow_mode"] is None


def test_bulk_import_multi_asset_pipe_separator(app_config) -> None:
    """``a.png|b.png`` in source_asset_path → both attached as ordered assets."""
    csv_text = (
        "sku_id,creative_id,segment_id,video_prompt,target_count,"
        "source_asset_path,asset_kind\n"
        "s,multi,A,p,2,a.png|b.png,reference\n"
    )
    with _client(app_config) as client:
        files = [
            ("csv_file", ("tasks.csv", io.BytesIO(csv_text.encode("utf-8")), "text/csv")),
            ("images", ("a.png", io.BytesIO(_PNG), "image/png")),
            ("images", ("b.png", io.BytesIO(_PNG), "image/png")),
        ]
        resp = client.post("/api/tasks/bulk-import", files=files)
        assert resp.status_code == 201, resp.text
        tid = resp.json()["task_ids"][0]
        detail = client.get(f"/api/tasks/{tid}").json()
    assert len(detail["assets"]) == 2
    assert [a["order"] for a in detail["assets"]] == [1, 2]


def test_bulk_import_strips_path_prefix_in_csv(app_config) -> None:
    """Customer pastes ``input/images/foo.png`` in CSV — bulk should
    match by basename so the user doesn't have to rewrite paths."""
    csv_text = (
        "sku_id,creative_id,segment_id,video_prompt,target_count,source_asset_path\n"
        "s,prefix,A,p,2,input/images/foo.png\n"
    )
    with _client(app_config) as client:
        files = [
            ("csv_file", ("tasks.csv", io.BytesIO(csv_text.encode("utf-8")), "text/csv")),
            ("images", ("foo.png", io.BytesIO(_PNG), "image/png")),
        ]
        resp = client.post("/api/tasks/bulk-import", files=files)
    assert resp.status_code == 201
    assert resp.json()["inserted"] == 1


def test_bulk_import_accepts_legacy_source_asset_type_alias(app_config) -> None:
    """Old CLI-format CSVs use ``source_asset_type`` instead of
    ``asset_kind``; bulk should fall back to that alias."""
    csv_text = (
        "sku_id,creative_id,segment_id,video_prompt,target_count,"
        "source_asset_path,source_asset_type\n"
        "s,legacy,A,p,2,a.png,first_frame\n"
    )
    with _client(app_config) as client:
        files = [
            ("csv_file", ("tasks.csv", io.BytesIO(csv_text.encode("utf-8")), "text/csv")),
            ("images", ("a.png", io.BytesIO(_PNG), "image/png")),
        ]
        resp = client.post("/api/tasks/bulk-import", files=files)
        tid = resp.json()["task_ids"][0]
        detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["assets"][0]["kind"] == "first_frame"


def test_bulk_form_page_renders(app_config) -> None:
    with _client(app_config) as client:
        resp = client.get("/tasks/bulk")
    assert resp.status_code == 200
    assert "批量导入任务" in resp.text
    assert 'name="csv_file"' in resp.text

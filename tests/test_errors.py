"""T05 — error logging tests."""

from __future__ import annotations

import logging
from pathlib import Path

from app.db.connection import connect
from app.utils.errors import save_error_snapshot
from app.utils.paths import ensure_segment_layout


def _seed_task(conn) -> None:
    conn.execute(
        "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
        "source_asset_path, source_asset_type, video_prompt, target_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("T1", "sku", "cre", "A", "/x", "first_frame", "p", 4),
    )


def test_save_error_snapshot_writes_log_and_screenshot(db_path: Path, tmp_path: Path) -> None:
    seg = ensure_segment_layout(tmp_path / "out", "2026-04-28", "sku", "cre", "A")
    log = logging.getLogger("test")
    conn = connect(db_path)
    try:
        _seed_task(conn)

        def take(target: Path) -> None:
            target.write_bytes(b"\x89PNG")

        snap = save_error_snapshot(
            conn,
            log=log,
            task_id="T1",
            workstation_id="WS_A",
            generation_round=2,
            error_type="page_load_failed",
            error_message="boom",
            segment_dir=seg,
            take_screenshot_fn=take,
        )
        assert snap.screenshot_path is not None
        assert Path(snap.screenshot_path).exists()
        row = conn.execute(
            "SELECT * FROM error_logs WHERE id = ?", (snap.error_log_id,)
        ).fetchone()
        assert row["task_id"] == "T1"
        assert row["workstation_id"] == "WS_A"
        assert row["generation_round"] == 2
        assert row["error_type"] == "page_load_failed"
        assert row["screenshot_path"] is not None
    finally:
        conn.close()


def test_save_error_snapshot_screenshot_failure_does_not_swallow_error(
    db_path: Path, tmp_path: Path
) -> None:
    seg = ensure_segment_layout(tmp_path / "out", "2026-04-28", "sku", "cre", "A")
    log = logging.getLogger("test")
    conn = connect(db_path)
    try:
        _seed_task(conn)

        def fails(target: Path) -> None:
            raise RuntimeError("disk full")

        snap = save_error_snapshot(
            conn,
            log=log,
            task_id="T1",
            workstation_id="WS_A",
            generation_round=1,
            error_type="generation_failed",
            error_message="boom",
            segment_dir=seg,
            take_screenshot_fn=fails,
        )
        # error log row still present
        row = conn.execute(
            "SELECT * FROM error_logs WHERE id = ?", (snap.error_log_id,)
        ).fetchone()
        assert row is not None
        assert row["screenshot_path"] is None
    finally:
        conn.close()

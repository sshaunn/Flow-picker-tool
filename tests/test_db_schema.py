"""T02 — schema tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.connection import connect
from app.db.schema import init_schema


def test_schema_creates_required_tables(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    init_schema(db)
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert {"tasks", "workstations", "task_results", "error_logs"} <= names
    finally:
        conn.close()


def test_schema_has_expected_indexes(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    init_schema(db)
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "idx_tasks_creative_segment_status" in names
        assert "idx_tasks_status_created" in names
        assert "idx_task_results_task" in names
    finally:
        conn.close()


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    init_schema(db)
    init_schema(db)  # must not throw
    conn = connect(db)
    try:
        conn.execute("SELECT 1 FROM tasks").fetchall()
    finally:
        conn.close()


def test_tasks_pk_rejects_duplicate(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    init_schema(db)
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
            "source_asset_path, source_asset_type, video_prompt, target_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("T1", "sku", "cre", "A", "/x", "first_frame", "p", 4),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
                "source_asset_path, source_asset_type, video_prompt, target_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("T1", "sku", "cre", "A", "/x", "first_frame", "p", 4),
            )
    finally:
        conn.close()


def test_tasks_target_count_must_be_positive(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    init_schema(db)
    conn = connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
                "source_asset_path, source_asset_type, video_prompt, target_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("T1", "sku", "cre", "A", "/x", "first_frame", "p", 0),
            )
    finally:
        conn.close()


def test_task_results_unique_round_seq(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    init_schema(db)
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
            "source_asset_path, source_asset_type, video_prompt, target_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("T1", "sku", "cre", "A", "/x", "first_frame", "p", 4),
        )
        conn.execute(
            "INSERT INTO task_results (task_id, creative_id, segment_id, "
            "workstation_id, generation_round, sequence_no, video_file_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("T1", "cre", "A", "WS_A", 1, 1, "/v.mp4"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_results (task_id, creative_id, segment_id, "
                "workstation_id, generation_round, sequence_no, video_file_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("T1", "cre", "A", "WS_A", 1, 1, "/v2.mp4"),
            )
    finally:
        conn.close()

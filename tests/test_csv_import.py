"""T03 — CSV import tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.connection import connect
from app.tasks.importer import ImportError, import_tasks


def _csv(rows: list[dict], columns: list[str]) -> str:
    out = [",".join(columns)]
    for r in rows:
        line = ",".join(str(r.get(c, "")) for c in columns)
        out.append(line)
    return "\n".join(out) + "\n"


HEADER = [
    "task_id", "sku_id", "creative_id", "segment_id", "sequence_index",
    "source_asset_path", "source_asset_type", "video_prompt", "target_count",
    "depends_on_task_id", "max_retry_count",
]


def _img(tmp_path: Path, name: str) -> Path:
    p = tmp_path / "input" / "images" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG")
    return p


def test_import_two_segments_pending(tmp_path: Path, db_path: Path) -> None:
    img_a = _img(tmp_path, "A.png")
    img_b = _img(tmp_path, "B.png")
    rows = [
        {
            "task_id": "T1", "sku_id": "sku1", "creative_id": "sku1_creative_001",
            "segment_id": "A", "sequence_index": 1,
            "source_asset_path": str(img_a),
            "source_asset_type": "first_frame",
            "video_prompt": "p", "target_count": 8,
        },
        {
            "task_id": "T2", "sku_id": "sku1", "creative_id": "sku1_creative_001",
            "segment_id": "B", "sequence_index": 2,
            "source_asset_path": str(img_b),
            "source_asset_type": "first_frame",
            "video_prompt": "p", "target_count": 8,
        },
    ]
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text(_csv(rows, HEADER), encoding="utf-8")
    summary = import_tasks(csv_path, db_path, default_max_retry=2)
    assert summary.inserted == 2
    conn = connect(db_path)
    try:
        result = conn.execute(
            "SELECT task_id, status, max_retry_count FROM tasks ORDER BY task_id"
        ).fetchall()
        assert [r["task_id"] for r in result] == ["T1", "T2"]
        assert all(r["status"] == "pending" for r in result)
        assert all(r["max_retry_count"] == 2 for r in result)
    finally:
        conn.close()


def test_import_default_max_retry_when_column_missing(tmp_path: Path, db_path: Path) -> None:
    img = _img(tmp_path, "A.png")
    rows = [
        {
            "task_id": "T1", "sku_id": "sku", "creative_id": "cre", "segment_id": "A",
            "sequence_index": 1, "source_asset_path": str(img),
            "source_asset_type": "first_frame", "video_prompt": "p", "target_count": 4,
        }
    ]
    columns_no_retry = HEADER[:-1]  # drop max_retry_count column entirely
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text(_csv(rows, columns_no_retry), encoding="utf-8")
    import_tasks(csv_path, db_path, default_max_retry=2)
    conn = connect(db_path)
    try:
        v = conn.execute("SELECT max_retry_count FROM tasks WHERE task_id='T1'").fetchone()
        assert v["max_retry_count"] == 2
    finally:
        conn.close()


def test_import_csv_overrides_max_retry(tmp_path: Path, db_path: Path) -> None:
    img = _img(tmp_path, "A.png")
    rows = [
        {
            "task_id": "T1", "sku_id": "sku", "creative_id": "cre", "segment_id": "A",
            "sequence_index": 1, "source_asset_path": str(img),
            "source_asset_type": "first_frame", "video_prompt": "p", "target_count": 4,
            "max_retry_count": 5,
        }
    ]
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text(_csv(rows, HEADER), encoding="utf-8")
    import_tasks(csv_path, db_path, default_max_retry=2)
    conn = connect(db_path)
    try:
        v = conn.execute("SELECT max_retry_count FROM tasks WHERE task_id='T1'").fetchone()
        assert v["max_retry_count"] == 5
    finally:
        conn.close()


def test_import_rejects_missing_asset(tmp_path: Path, db_path: Path) -> None:
    rows = [
        {
            "task_id": "T1", "sku_id": "sku", "creative_id": "cre", "segment_id": "A",
            "sequence_index": 1, "source_asset_path": str(tmp_path / "missing.png"),
            "source_asset_type": "first_frame", "video_prompt": "p", "target_count": 4,
        }
    ]
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text(_csv(rows, HEADER), encoding="utf-8")
    with pytest.raises(ImportError, match="line 2"):
        import_tasks(csv_path, db_path, default_max_retry=2)


def test_import_rejects_target_count_zero(tmp_path: Path, db_path: Path) -> None:
    img = _img(tmp_path, "A.png")
    rows = [
        {
            "task_id": "T1", "sku_id": "sku", "creative_id": "cre", "segment_id": "A",
            "sequence_index": 1, "source_asset_path": str(img),
            "source_asset_type": "first_frame", "video_prompt": "p", "target_count": 0,
        }
    ]
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text(_csv(rows, HEADER), encoding="utf-8")
    with pytest.raises(ImportError, match="target_count must be > 0"):
        import_tasks(csv_path, db_path, default_max_retry=2)


def test_import_rejects_bad_asset_type(tmp_path: Path, db_path: Path) -> None:
    img = _img(tmp_path, "A.png")
    rows = [
        {
            "task_id": "T1", "sku_id": "sku", "creative_id": "cre", "segment_id": "A",
            "sequence_index": 1, "source_asset_path": str(img),
            "source_asset_type": "wrong_type", "video_prompt": "p", "target_count": 4,
        }
    ]
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text(_csv(rows, HEADER), encoding="utf-8")
    with pytest.raises(ImportError, match="line 2"):
        import_tasks(csv_path, db_path, default_max_retry=2)


def test_import_rejects_negative_max_retry_no_default(tmp_path: Path, db_path: Path) -> None:
    img = _img(tmp_path, "A.png")
    rows = [
        {
            "task_id": "T1", "sku_id": "sku", "creative_id": "cre", "segment_id": "A",
            "sequence_index": 1, "source_asset_path": str(img),
            "source_asset_type": "first_frame", "video_prompt": "p",
            "target_count": 4, "max_retry_count": -1,
        }
    ]
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text(_csv(rows, HEADER), encoding="utf-8")
    with pytest.raises(ImportError, match="line 2"):
        import_tasks(csv_path, db_path, default_max_retry=2)


def test_import_blocks_active_collision(tmp_path: Path, db_path: Path) -> None:
    img = _img(tmp_path, "A.png")
    rows = [
        {
            "task_id": "T1", "sku_id": "sku", "creative_id": "cre", "segment_id": "A",
            "sequence_index": 1, "source_asset_path": str(img),
            "source_asset_type": "first_frame", "video_prompt": "p", "target_count": 4,
        }
    ]
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text(_csv(rows, HEADER), encoding="utf-8")
    import_tasks(csv_path, db_path, default_max_retry=2)
    rows[0]["task_id"] = "T2"
    csv_path.write_text(_csv(rows, HEADER), encoding="utf-8")
    with pytest.raises(ImportError, match="active task already exists"):
        import_tasks(csv_path, db_path, default_max_retry=2)


def test_import_multiple_assets_with_pipe(tmp_path: Path, db_path: Path) -> None:
    """Pipe-delimited paths should produce multiple ordered task_assets rows."""
    img_a = _img(tmp_path, "first.png")
    img_b = _img(tmp_path, "last.png")
    rows = [
        {
            "task_id": "T1", "sku_id": "sku", "creative_id": "cre", "segment_id": "A",
            "sequence_index": 1,
            "source_asset_path": f"{img_a}|{img_b}",
            "source_asset_type": "first_frame|last_frame",
            "video_prompt": "p", "target_count": 4,
        }
    ]
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text(_csv(rows, HEADER), encoding="utf-8")
    summary = import_tasks(csv_path, db_path, default_max_retry=2)
    assert summary.inserted == 1
    conn = connect(db_path)
    try:
        assets = conn.execute(
            "SELECT asset_order, asset_path, asset_type FROM task_assets "
            "WHERE task_id='T1' ORDER BY asset_order"
        ).fetchall()
        assert len(assets) == 2
        assert assets[0]["asset_order"] == 1
        assert assets[0]["asset_path"].endswith("first.png")
        assert assets[0]["asset_type"] == "first_frame"
        assert assets[1]["asset_order"] == 2
        assert assets[1]["asset_path"].endswith("last.png")
        assert assets[1]["asset_type"] == "last_frame"
        # Legacy column mirrors the FIRST asset
        legacy = conn.execute(
            "SELECT source_asset_path, source_asset_type FROM tasks WHERE task_id='T1'"
        ).fetchone()
        assert legacy["source_asset_path"].endswith("first.png")
        assert legacy["source_asset_type"] == "first_frame"
    finally:
        conn.close()


def test_import_broadcasts_single_type_to_many_paths(tmp_path: Path, db_path: Path) -> None:
    """One ``source_asset_type`` should broadcast to all pipe-split paths
    — common for Ingredients mode where every image is 'reference'."""
    a = _img(tmp_path, "a.png"); b = _img(tmp_path, "b.png"); c = _img(tmp_path, "c.png")
    rows = [
        {
            "task_id": "T1", "sku_id": "sku", "creative_id": "cre", "segment_id": "A",
            "sequence_index": 1,
            "source_asset_path": f"{a}|{b}|{c}",
            "source_asset_type": "reference",
            "video_prompt": "p", "target_count": 4,
        }
    ]
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text(_csv(rows, HEADER), encoding="utf-8")
    import_tasks(csv_path, db_path, default_max_retry=2)
    conn = connect(db_path)
    try:
        kinds = [
            r["asset_type"] for r in conn.execute(
                "SELECT asset_type FROM task_assets WHERE task_id='T1' ORDER BY asset_order"
            )
        ]
        assert kinds == ["reference", "reference", "reference"]
    finally:
        conn.close()


def test_import_rejects_mismatched_path_type_count(tmp_path: Path, db_path: Path) -> None:
    a = _img(tmp_path, "a.png"); b = _img(tmp_path, "b.png")
    rows = [
        {
            "task_id": "T1", "sku_id": "sku", "creative_id": "cre", "segment_id": "A",
            "sequence_index": 1,
            "source_asset_path": f"{a}|{b}",
            "source_asset_type": "first_frame|last_frame|reference",  # 3 vs 2
            "video_prompt": "p", "target_count": 4,
        }
    ]
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text(_csv(rows, HEADER), encoding="utf-8")
    with pytest.raises(ImportError, match="counts must match"):
        import_tasks(csv_path, db_path, default_max_retry=2)


def test_import_allows_after_terminal(tmp_path: Path, db_path: Path) -> None:
    img = _img(tmp_path, "A.png")
    rows = [
        {
            "task_id": "T1", "sku_id": "sku", "creative_id": "cre", "segment_id": "A",
            "sequence_index": 1, "source_asset_path": str(img),
            "source_asset_type": "first_frame", "video_prompt": "p", "target_count": 4,
        }
    ]
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text(_csv(rows, HEADER), encoding="utf-8")
    import_tasks(csv_path, db_path, default_max_retry=2)
    conn = connect(db_path)
    try:
        conn.execute("UPDATE tasks SET status='failed' WHERE task_id='T1'")
    finally:
        conn.close()
    rows[0]["task_id"] = "T2"
    csv_path.write_text(_csv(rows, HEADER), encoding="utf-8")
    summary = import_tasks(csv_path, db_path, default_max_retry=2)
    assert summary.inserted == 1

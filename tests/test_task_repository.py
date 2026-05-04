"""Form-style task creation API."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import paths as app_paths
from app.db.connection import connect
from app.tasks.repository import (
    AssetDraft,
    TaskDraft,
    TaskRepositoryError,
    create_task,
    delete_task,
    generate_task_id,
    get_task,
    get_task_assets,
    list_tasks,
)


@pytest.fixture(autouse=True)
def _redirect_assets_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep the managed assets dir inside tmp_path for the duration of each test
    so we don't clobber the dev install's real ~/Library/Application Support."""
    monkeypatch.setenv(app_paths._ENV_DATA_DIR, str(tmp_path / "data"))


def _make_image(tmp_path: Path, name: str = "img.png") -> Path:
    p = tmp_path / "input" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


def _draft(tmp_path: Path, **overrides) -> TaskDraft:
    base = TaskDraft(
        sku_id="sku_001",
        creative_id="creative_001",
        segment_id="A",
        video_prompt="a serene beach at sunset",
        target_count=4,
        assets=[AssetDraft(path=_make_image(tmp_path), kind="reference")],
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_generate_task_id_format() -> None:
    tid = generate_task_id()
    assert tid.startswith("T_")
    parts = tid.split("_")
    assert len(parts) == 3
    assert len(parts[1]) == 15  # YYYYMMDDTHHMMSS
    assert len(parts[2]) == 6   # 3 hex bytes


def test_create_task_basic_round_trip(db_path: Path, tmp_path: Path) -> None:
    with connect(db_path) as conn:
        new_id = create_task(conn, _draft(tmp_path))
        record = get_task(conn, new_id)
    assert record is not None
    assert record.status == "pending"
    assert record.target_count == 4
    assert record.downloaded_count == 0
    assert record.creative_id == "creative_001"


def test_create_task_copies_asset_into_managed_dir(
    db_path: Path, tmp_path: Path
) -> None:
    src = _make_image(tmp_path, "original.png")
    with connect(db_path) as conn:
        new_id = create_task(
            conn,
            _draft(tmp_path, assets=[AssetDraft(path=src, kind="reference")]),
        )
        assets = get_task_assets(conn, new_id)
    assert len(assets) == 1
    order, path, kind = assets[0]
    assert order == 1
    assert kind == "reference"
    # Asset was COPIED, not referenced — original survives, dest exists.
    assert Path(path).exists()
    assert src.exists()
    # Dest sits under the managed assets dir.
    expected_root = app_paths.assets_dir() / new_id
    assert Path(path).parent == expected_root
    assert "original" in Path(path).name


def test_create_task_no_copy_keeps_original_path(
    db_path: Path, tmp_path: Path
) -> None:
    src = _make_image(tmp_path, "stay.png")
    with connect(db_path) as conn:
        new_id = create_task(
            conn,
            _draft(tmp_path, assets=[
                AssetDraft(path=src, kind="reference", copy_into_managed_dir=False),
            ]),
        )
        assets = get_task_assets(conn, new_id)
    assert assets[0][1] == str(src)


def test_create_task_multiple_assets_preserves_order(
    db_path: Path, tmp_path: Path
) -> None:
    images = [_make_image(tmp_path, f"img_{i}.png") for i in range(3)]
    drafts = [
        AssetDraft(path=p, kind="first_frame" if i == 0 else "reference")
        for i, p in enumerate(images)
    ]
    with connect(db_path) as conn:
        new_id = create_task(conn, _draft(tmp_path, assets=drafts))
        assets = get_task_assets(conn, new_id)
    assert [order for order, _, _ in assets] == [1, 2, 3]
    assert assets[0][2] == "first_frame"
    # tasks.source_asset_path mirrors the FIRST asset (legacy contract).
    record = get_task(connect(db_path), new_id)
    assert record is not None
    assert "img_0" in record.source_asset_path
    assert record.source_asset_type == "first_frame"


def test_create_task_explicit_id(db_path: Path, tmp_path: Path) -> None:
    with connect(db_path) as conn:
        new_id = create_task(conn, _draft(tmp_path, task_id="T_CUSTOM_ID"))
    assert new_id == "T_CUSTOM_ID"


def test_create_task_persists_flow_mode_override(
    db_path: Path, tmp_path: Path
) -> None:
    """Per-task flow_mode is round-tripped through the DB."""
    from app.config.loader import FlowModeSpec

    fm = FlowModeSpec(
        model="Veo 3.1 - Quality", output_count=2, duration_sec=4, aspect="1:1",
    )
    with connect(db_path) as conn:
        new_id = create_task(conn, _draft(tmp_path, flow_mode=fm))
        record = get_task(conn, new_id)
    assert record is not None
    assert record.flow_mode is not None
    assert record.flow_mode.model == "Veo 3.1 - Quality"
    assert record.flow_mode.output_count == 2
    assert record.flow_mode.duration_sec == 4
    assert record.flow_mode.aspect == "1:1"
    # Tab/subtab left None on the draft -> still None on the record.
    assert record.flow_mode.tab is None


def test_create_task_no_flow_mode_record_is_none(
    db_path: Path, tmp_path: Path
) -> None:
    with connect(db_path) as conn:
        new_id = create_task(conn, _draft(tmp_path))
        record = get_task(conn, new_id)
    assert record is not None and record.flow_mode is None


def test_create_task_rejects_zero_target(db_path: Path, tmp_path: Path) -> None:
    with connect(db_path) as conn:
        with pytest.raises(TaskRepositoryError, match="target_count"):
            create_task(conn, _draft(tmp_path, target_count=0))


def test_create_task_rejects_empty_prompt(db_path: Path, tmp_path: Path) -> None:
    with connect(db_path) as conn:
        with pytest.raises(TaskRepositoryError, match="video_prompt"):
            create_task(conn, _draft(tmp_path, video_prompt="   "))


def test_create_task_rejects_no_assets(db_path: Path, tmp_path: Path) -> None:
    with connect(db_path) as conn:
        with pytest.raises(TaskRepositoryError, match="at least one source asset"):
            create_task(conn, _draft(tmp_path, assets=[]))


def test_create_task_rejects_missing_asset_file(
    db_path: Path, tmp_path: Path
) -> None:
    with connect(db_path) as conn:
        with pytest.raises(TaskRepositoryError, match="does not exist"):
            create_task(conn, _draft(tmp_path, assets=[
                AssetDraft(path=tmp_path / "nope.png", kind="reference"),
            ]))


def test_create_task_rejects_path_traversal_in_segment(
    db_path: Path, tmp_path: Path
) -> None:
    with connect(db_path) as conn:
        with pytest.raises(TaskRepositoryError, match="illegal token"):
            create_task(conn, _draft(tmp_path, segment_id="../A"))


def test_create_task_rejects_collision_with_active_task(
    db_path: Path, tmp_path: Path
) -> None:
    with connect(db_path) as conn:
        create_task(conn, _draft(tmp_path))
        # Same (creative_id, segment_id) -> conflict
        with pytest.raises(TaskRepositoryError, match="active task already exists"):
            create_task(conn, _draft(tmp_path))


def test_create_task_allowed_after_terminal(
    db_path: Path, tmp_path: Path
) -> None:
    with connect(db_path) as conn:
        first = create_task(conn, _draft(tmp_path))
        conn.execute(
            "UPDATE tasks SET status = 'success' WHERE task_id = ?", (first,)
        )
        conn.commit()
        # Same (creative, segment) is allowed once the prior is terminal.
        second = create_task(conn, _draft(tmp_path))
    assert second != first


def test_invalid_asset_kind_raises_at_construction() -> None:
    with pytest.raises(TaskRepositoryError, match="asset kind"):
        AssetDraft(path=Path("/tmp/x"), kind="not_a_real_kind")


def test_list_tasks_filters_by_status(db_path: Path, tmp_path: Path) -> None:
    with connect(db_path) as conn:
        a = create_task(conn, _draft(tmp_path))
        # Mark a terminal so we can drop a second draft for the same segment.
        conn.execute("UPDATE tasks SET status = 'success' WHERE task_id = ?", (a,))
        conn.commit()
        b = create_task(conn, _draft(tmp_path))

        all_tasks = list_tasks(conn)
        pending_only = list_tasks(conn, status="pending")
        success_only = list_tasks(conn, status="success")

    assert {t.task_id for t in all_tasks} == {a, b}
    assert [t.task_id for t in pending_only] == [b]
    assert [t.task_id for t in success_only] == [a]


def test_list_tasks_respects_limit(db_path: Path, tmp_path: Path) -> None:
    with connect(db_path) as conn:
        for i in range(5):
            d = _draft(tmp_path, segment_id=chr(ord("A") + i))
            tid = create_task(conn, d)
            conn.execute("UPDATE tasks SET status = 'success' WHERE task_id = ?", (tid,))
            conn.commit()
        rows = list_tasks(conn, limit=2)
    assert len(rows) == 2


def test_get_task_returns_none_when_missing(db_path: Path) -> None:
    with connect(db_path) as conn:
        assert get_task(conn, "nope") is None


def test_delete_task_removes_row_and_assets_dir(
    db_path: Path, tmp_path: Path
) -> None:
    with connect(db_path) as conn:
        new_id = create_task(conn, _draft(tmp_path))
    asset_dir = app_paths.assets_dir() / new_id
    assert asset_dir.exists()

    with connect(db_path) as conn:
        ok = delete_task(conn, new_id)
        gone = get_task(conn, new_id)
    assert ok is True
    assert gone is None
    assert not asset_dir.exists()


def test_delete_task_keeps_assets_when_requested(
    db_path: Path, tmp_path: Path
) -> None:
    with connect(db_path) as conn:
        new_id = create_task(conn, _draft(tmp_path))
        asset_dir = app_paths.assets_dir() / new_id
        delete_task(conn, new_id, remove_assets=False)
    assert asset_dir.exists()


def test_delete_task_running_requires_force(
    db_path: Path, tmp_path: Path
) -> None:
    with connect(db_path) as conn:
        new_id = create_task(conn, _draft(tmp_path))
        conn.execute("UPDATE tasks SET status = 'running' WHERE task_id = ?", (new_id,))
        conn.commit()
        with pytest.raises(TaskRepositoryError, match="currently running"):
            delete_task(conn, new_id)
        # force overrides
        ok = delete_task(conn, new_id, force=True)
    assert ok is True


def test_delete_task_returns_false_when_missing(db_path: Path) -> None:
    with connect(db_path) as conn:
        assert delete_task(conn, "nope") is False


def test_resume_task_clears_retry_keeps_progress(
    db_path: Path, tmp_path: Path
) -> None:
    """Customer-facing 继续任务: clear retry counter + status, keep progress."""
    from app.tasks.repository import resume_task

    with connect(db_path) as conn:
        new_id = create_task(conn, _draft(tmp_path))
        # Simulate a worker that ran twice + persisted some downloads.
        conn.execute(
            "UPDATE tasks SET status='retry_waiting', retry_count=2, "
            "max_retry_count=2, downloaded_count=15, generation_round_count=8, "
            "error_type='unusual_activity', error_message='hit rate limit', "
            "assigned_workstation_id='WS_A' WHERE task_id=?",
            (new_id,),
        )
        conn.commit()

        ok = resume_task(conn, new_id)
        record = get_task(conn, new_id)

    assert ok is True
    assert record is not None
    assert record.status == "pending"
    assert record.retry_count == 0
    assert record.error_type is None
    assert record.error_message is None
    # Downloaded progress is preserved so the next claim continues from
    # where the earlier attempts left off.
    assert record.downloaded_count == 15
    # ``generation_round_count`` MUST reset to 0 — otherwise a task that
    # already hit ``max_round_per_task`` would resume into the worker
    # loop, fail the round-cap check on the very first iteration, and
    # mark itself failed in 5 seconds without generating anything (the
    # original "继续任务 没用" bug).
    assert record.generation_round_count == 0


def test_resume_task_refuses_running(db_path: Path, tmp_path: Path) -> None:
    """Don't race the worker — a running task can't be resumed."""
    from app.tasks.repository import resume_task

    with connect(db_path) as conn:
        new_id = create_task(conn, _draft(tmp_path))
        conn.execute(
            "UPDATE tasks SET status = 'running' WHERE task_id = ?", (new_id,),
        )
        conn.commit()
        assert resume_task(conn, new_id) is False


def test_resume_task_refuses_success(db_path: Path, tmp_path: Path) -> None:
    """Nothing to continue on a completed task."""
    from app.tasks.repository import resume_task

    with connect(db_path) as conn:
        new_id = create_task(conn, _draft(tmp_path))
        conn.execute(
            "UPDATE tasks SET status = 'success' WHERE task_id = ?", (new_id,),
        )
        conn.commit()
        assert resume_task(conn, new_id) is False


def test_resume_task_returns_false_for_missing(db_path: Path) -> None:
    from app.tasks.repository import resume_task
    with connect(db_path) as conn:
        assert resume_task(conn, "nope") is False


def test_delete_task_cascades_task_assets(db_path: Path, tmp_path: Path) -> None:
    """task_assets has FK ON DELETE CASCADE — verify rows actually vanish."""
    with connect(db_path) as conn:
        new_id = create_task(conn, _draft(tmp_path))
        # SQLite needs PRAGMA foreign_keys=ON for CASCADE to fire.
        conn.execute("PRAGMA foreign_keys=ON")
        delete_task(conn, new_id, remove_assets=False)
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM task_assets WHERE task_id = ?", (new_id,)
        ).fetchone()
    assert rows["n"] == 0

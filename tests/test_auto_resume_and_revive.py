"""Auto-resume retry-exhausted tasks + revive workstation health."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.connection import connect
from app.tasks.repository import (
    AssetDraft,
    TaskDraft,
    auto_resume_exhausted_tasks,
    create_task,
    get_task,
    resume_task,
)
from app.workstations.repository import (
    create_workstation,
    get_workstation_health,
    revive_workstation,
)
from app.config.loader import WorkstationConfig


def _ws(id_: str = "WS_X") -> WorkstationConfig:
    return WorkstationConfig(
        id=id_, account_label="x", browser_profile_path="/tmp/x",
        daily_task_limit=20,
    )


def _task(tmp_path: Path, *, segment: str = "A") -> TaskDraft:
    img = tmp_path / "i.png"
    img.write_bytes(b"\x89PNG")
    return TaskDraft(
        sku_id="s", creative_id="c", segment_id=segment,
        video_prompt="p", target_count=4,
        assets=[AssetDraft(path=img, copy_into_managed_dir=False)],
    )


# ----------------------------------------------------- revive_workstation


def test_revive_workstation_clears_runtime_state(db_path: Path) -> None:
    """Re-login should wipe strikes / cooldown / consecutive failures so
    the customer's only path back from manual_check actually works."""
    with connect(db_path) as conn:
        create_workstation(conn, _ws())
        conn.execute(
            """
            UPDATE workstations SET
                status = 'manual_check',
                ban_probe_count = 5,
                cooldown_until = '2099-01-01 00:00:00',
                cooldown_reason = 'unusual_activity_strike_5',
                consecutive_failure_count = 7
             WHERE id = 'WS_X'
            """
        )
        conn.commit()

        ok = revive_workstation(conn, "WS_X")
        h = get_workstation_health(conn, "WS_X")
        status = conn.execute(
            "SELECT status FROM workstations WHERE id = 'WS_X'"
        ).fetchone()["status"]

    assert ok is True
    assert status == "healthy"
    assert h is not None
    assert h.ban_probe_count == 0
    assert h.consecutive_failure_count == 0
    assert h.cooldown_until is None
    assert h.cooldown_reason is None


def test_revive_workstation_returns_false_for_missing(db_path: Path) -> None:
    with connect(db_path) as conn:
        assert revive_workstation(conn, "NOPE") is False


# ----------------------------------------------------- auto_resume


def test_auto_resume_picks_up_exhausted_tasks(
    db_path: Path, tmp_path: Path
) -> None:
    """When a healthy WS exists and a task is at retry-exhaustion, auto
    resume should reset retry_count + flip to pending."""
    with connect(db_path) as conn:
        create_workstation(conn, _ws("WS_HEALTHY"))
        new_id = create_task(conn, _task(tmp_path))
        # Simulate a task that hit max_round_per_task (20) — without the
        # round-count reset on resume the worker would exit on round 1
        # of the resumed run before generating anything.
        conn.execute(
            "UPDATE tasks SET status='retry_waiting', retry_count=2, "
            "max_retry_count=2, downloaded_count=18, "
            "generation_round_count=20 WHERE task_id=?",
            (new_id,),
        )
        conn.commit()

        n = auto_resume_exhausted_tasks(conn, max_auto_resume_count=3)
        record = get_task(conn, new_id)
        bumped = conn.execute(
            "SELECT auto_resumed_count FROM tasks WHERE task_id = ?", (new_id,)
        ).fetchone()["auto_resumed_count"]

    assert n == 1
    assert record is not None
    assert record.status == "pending"
    assert record.retry_count == 0
    assert record.downloaded_count == 18  # download progress preserved
    # Round budget reset — task gets a fresh max_round_per_task to fill
    # the remaining 2 of 20.
    assert record.generation_round_count == 0
    assert bumped == 1


def test_auto_resume_skips_when_no_healthy_workstation(
    db_path: Path, tmp_path: Path
) -> None:
    with connect(db_path) as conn:
        create_workstation(conn, _ws("WS_COOL"))
        conn.execute(
            "UPDATE workstations SET status='cooldown' WHERE id='WS_COOL'"
        )
        new_id = create_task(conn, _task(tmp_path))
        conn.execute(
            "UPDATE tasks SET status='retry_waiting', retry_count=2, "
            "max_retry_count=2 WHERE task_id=?", (new_id,),
        )
        conn.commit()

        n = auto_resume_exhausted_tasks(conn, max_auto_resume_count=3)
        record = get_task(conn, new_id)

    assert n == 0
    assert record.status == "retry_waiting"


def test_auto_resume_respects_cap(db_path: Path, tmp_path: Path) -> None:
    """Once a task hits the auto-resume cap, the auto path stops."""
    with connect(db_path) as conn:
        create_workstation(conn, _ws())
        new_id = create_task(conn, _task(tmp_path))
        conn.execute(
            "UPDATE tasks SET status='retry_waiting', retry_count=2, "
            "max_retry_count=2, auto_resumed_count=3 WHERE task_id=?",
            (new_id,),
        )
        conn.commit()
        n = auto_resume_exhausted_tasks(conn, max_auto_resume_count=3)
    assert n == 0


def test_manual_resume_clears_auto_resume_count(
    db_path: Path, tmp_path: Path
) -> None:
    """Customer clicking 继续任务 grants a fresh auto-resume budget."""
    with connect(db_path) as conn:
        create_workstation(conn, _ws())
        new_id = create_task(conn, _task(tmp_path))
        conn.execute(
            "UPDATE tasks SET status='retry_waiting', retry_count=2, "
            "max_retry_count=2, auto_resumed_count=3 WHERE task_id=?",
            (new_id,),
        )
        conn.commit()

        assert resume_task(conn, new_id) is True
        bumped = conn.execute(
            "SELECT auto_resumed_count FROM tasks WHERE task_id = ?",
            (new_id,),
        ).fetchone()["auto_resumed_count"]
    assert bumped == 0


def test_auto_resume_cap_zero_disables_feature(
    db_path: Path, tmp_path: Path
) -> None:
    with connect(db_path) as conn:
        create_workstation(conn, _ws())
        new_id = create_task(conn, _task(tmp_path))
        conn.execute(
            "UPDATE tasks SET status='retry_waiting', retry_count=2, "
            "max_retry_count=2 WHERE task_id=?", (new_id,),
        )
        conn.commit()
        n = auto_resume_exhausted_tasks(conn, max_auto_resume_count=0)
    assert n == 0

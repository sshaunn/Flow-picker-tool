"""DB-first CRUD for workstations."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.loader import FlowModeSpec, WorkstationConfig
from app.db.connection import connect
from app.workstations.repository import (
    WorkstationConflictError,
    WorkstationNotFoundError,
    create_workstation,
    delete_workstation,
    get_workstation,
    list_workstations,
    update_workstation_config,
    upsert_workstation,
)


def _make_ws(
    ws_id: str = "WS_A",
    *,
    profile: str = "/tmp/profile",
    flow_project_url: str | None = None,
    flow_mode: FlowModeSpec | None = None,
) -> WorkstationConfig:
    return WorkstationConfig(
        id=ws_id,
        account_label=f"acct_{ws_id}",
        browser_profile_path=profile,
        daily_task_limit=20,
        flow_project_url=flow_project_url,
        flow_mode=flow_mode,
    )


def test_list_empty_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        assert list_workstations(conn) == []


def test_create_and_get_round_trip(db_path: Path) -> None:
    ws = _make_ws(
        flow_project_url="https://labs.google/fx/tools/flow/project/abc",
        flow_mode=FlowModeSpec(
            tab="video", subtab="ingredients", aspect="9:16",
            output_count=1, duration_sec=8, model="Veo 3.1 - Fast",
        ),
    )
    with connect(db_path) as conn:
        create_workstation(conn, ws)
        loaded = get_workstation(conn, "WS_A")
    assert loaded is not None
    assert loaded.id == "WS_A"
    assert loaded.account_label == "acct_WS_A"
    assert loaded.daily_task_limit == 20
    assert loaded.flow_project_url == "https://labs.google/fx/tools/flow/project/abc"
    assert loaded.flow_mode is not None
    assert loaded.flow_mode.aspect == "9:16"
    assert loaded.flow_mode.duration_sec == 8


def test_create_with_no_flow_mode_persists_null(db_path: Path) -> None:
    with connect(db_path) as conn:
        create_workstation(conn, _make_ws(flow_mode=None))
        loaded = get_workstation(conn, "WS_A")
    assert loaded is not None
    assert loaded.flow_mode is None


def test_create_duplicate_raises(db_path: Path) -> None:
    with connect(db_path) as conn:
        create_workstation(conn, _make_ws())
        with pytest.raises(WorkstationConflictError):
            create_workstation(conn, _make_ws())


def test_list_orders_by_id(db_path: Path) -> None:
    with connect(db_path) as conn:
        create_workstation(conn, _make_ws("WS_C"))
        create_workstation(conn, _make_ws("WS_A"))
        create_workstation(conn, _make_ws("WS_B"))
        ids = [w.id for w in list_workstations(conn)]
    assert ids == ["WS_A", "WS_B", "WS_C"]


def test_upsert_inserts_then_updates(db_path: Path) -> None:
    with connect(db_path) as conn:
        assert upsert_workstation(conn, _make_ws()) == "inserted"
        # Second call with mutated config -> update
        ws2 = _make_ws(profile="/tmp/profile-v2")
        assert upsert_workstation(conn, ws2) == "updated"
        loaded = get_workstation(conn, "WS_A")
    assert loaded is not None
    assert loaded.browser_profile_path == "/tmp/profile-v2"


def test_upsert_preserves_runtime_state(db_path: Path) -> None:
    """status / cooldown / counters must NEVER be clobbered by config sync."""
    with connect(db_path) as conn:
        create_workstation(conn, _make_ws())
        # Simulate the scheduler flipping the WS to manual_check after a ban.
        conn.execute(
            """
            UPDATE workstations SET
                status = 'manual_check',
                ban_probe_count = 4,
                today_failure_count = 7,
                cooldown_until = '2099-01-01 00:00:00',
                cooldown_reason = 'unusual_activity'
             WHERE id = ?
            """,
            ("WS_A",),
        )
        conn.commit()

        # Now re-upsert with a fresh config (simulating yaml resync / Web edit)
        upsert_workstation(conn, _make_ws(profile="/tmp/profile-v2"))

        row = conn.execute(
            "SELECT status, ban_probe_count, today_failure_count, cooldown_until, "
            "cooldown_reason, browser_profile_path FROM workstations WHERE id = 'WS_A'"
        ).fetchone()
    assert row["status"] == "manual_check"
    assert row["ban_probe_count"] == 4
    assert row["today_failure_count"] == 7
    assert row["cooldown_until"] == "2099-01-01 00:00:00"
    assert row["cooldown_reason"] == "unusual_activity"
    # But config field DID get updated.
    assert row["browser_profile_path"] == "/tmp/profile-v2"


def test_update_workstation_config_partial(db_path: Path) -> None:
    with connect(db_path) as conn:
        create_workstation(conn, _make_ws())
        update_workstation_config(conn, "WS_A", daily_task_limit=99)
        loaded = get_workstation(conn, "WS_A")
    assert loaded is not None
    assert loaded.daily_task_limit == 99
    assert loaded.account_label == "acct_WS_A"  # unchanged


def test_update_workstation_config_flow_mode(db_path: Path) -> None:
    with connect(db_path) as conn:
        create_workstation(conn, _make_ws())
        update_workstation_config(
            conn, "WS_A",
            flow_mode=FlowModeSpec(tab="video", aspect="16:9", duration_sec=4),
        )
        loaded = get_workstation(conn, "WS_A")
    assert loaded is not None
    assert loaded.flow_mode is not None
    assert loaded.flow_mode.aspect == "16:9"
    assert loaded.flow_mode.duration_sec == 4


def test_update_workstation_config_clears_flow_project_url(db_path: Path) -> None:
    with connect(db_path) as conn:
        create_workstation(conn, _make_ws(flow_project_url="https://x"))
        update_workstation_config(conn, "WS_A", flow_project_url=None)
        loaded = get_workstation(conn, "WS_A")
    assert loaded is not None
    assert loaded.flow_project_url is None


def test_update_workstation_config_unknown_field_raises(db_path: Path) -> None:
    with connect(db_path) as conn:
        create_workstation(conn, _make_ws())
        with pytest.raises(ValueError, match="not editable"):
            update_workstation_config(conn, "WS_A", status="manual_check")
        with pytest.raises(ValueError, match="not editable"):
            update_workstation_config(conn, "WS_A", ban_probe_count=99)


def test_update_workstation_config_missing_id_raises(db_path: Path) -> None:
    with connect(db_path) as conn:
        with pytest.raises(WorkstationNotFoundError):
            update_workstation_config(conn, "WS_X", daily_task_limit=5)


def test_delete_workstation(db_path: Path) -> None:
    with connect(db_path) as conn:
        create_workstation(conn, _make_ws())
        assert delete_workstation(conn, "WS_A") is True
        assert get_workstation(conn, "WS_A") is None
        # Second delete is a no-op, returns False.
        assert delete_workstation(conn, "WS_A") is False


def test_schema_migration_adds_new_columns_to_existing_db(tmp_path: Path) -> None:
    """init_schema is idempotent across the new flow_* migrations."""
    from app.db.schema import init_schema

    db = tmp_path / "fresh.sqlite"
    init_schema(db)
    init_schema(db)  # second pass must not raise duplicate-column

    with connect(db) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(workstations)")}
    for required in (
        "flow_project_url", "flow_mode_tab", "flow_mode_subtab",
        "flow_mode_aspect", "flow_mode_output_count",
        "flow_mode_duration_sec", "flow_mode_model",
    ):
        assert required in cols, f"missing migrated column: {required}"


def test_sync_workstations_uses_repository(tmp_path: Path) -> None:
    """The legacy yaml-bootstrap entrypoint now delegates to repository CRUD."""
    from app.db.schema import init_schema
    from app.workstations.sync import sync_workstations

    db = tmp_path / "sync.sqlite"
    init_schema(db)

    ws_list = [
        _make_ws("WS_A", flow_project_url="https://x/a"),
        _make_ws("WS_B", flow_mode=FlowModeSpec(tab="video", aspect="9:16")),
    ]
    inserted, updated = sync_workstations(db, ws_list)
    assert (inserted, updated) == (2, 0)

    # Re-running with same ids -> updated count grows, no duplicates.
    inserted, updated = sync_workstations(db, ws_list)
    assert (inserted, updated) == (0, 2)

    with connect(db) as conn:
        loaded = list_workstations(conn)
    assert [w.id for w in loaded] == ["WS_A", "WS_B"]
    assert loaded[0].flow_project_url == "https://x/a"
    assert loaded[1].flow_mode is not None
    assert loaded[1].flow_mode.aspect == "9:16"

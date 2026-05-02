"""DB-first CRUD for workstation records.

The customer-facing Web UI reads and mutates workstations through this
repository instead of editing ``config/workstations.yaml``.
``WorkstationConfig`` (the pydantic model from ``app.config.loader``) stays
the canonical in-memory shape so runners / tests don't have to learn a new
type — repository functions return / accept ``WorkstationConfig`` instances.

DB-only fields that aren't part of the user-editable config (``status``,
``today_*``, ``cooldown_*``, ``ban_probe_count``, ``last_*_at``) are NOT
overwritten by ``upsert_workstation`` — those belong to the scheduler and
the Web UI must not be able to clobber them. ``update_workstation_config``
likewise only touches the safe subset.

Usage:

    from app.workstations.repository import list_workstations, upsert_workstation
    with connect(db_path) as conn:
        for ws in list_workstations(conn):
            ...
        upsert_workstation(conn, new_ws)
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.config.loader import FlowModeSpec, WorkstationConfig
from app.db.connection import transaction


# Columns that describe user-editable workstation *config* (as opposed to
# scheduler runtime state). Limiting writes to this set prevents the Web
# UI from accidentally clobbering ``status`` / cooldown counters.
_EDITABLE_COLUMNS = (
    "account_label",
    "browser_profile_path",
    "daily_task_limit",
    "flow_project_url",
    "flow_mode_tab",
    "flow_mode_subtab",
    "flow_mode_aspect",
    "flow_mode_output_count",
    "flow_mode_duration_sec",
    "flow_mode_model",
)


class WorkstationNotFoundError(LookupError):
    """Raised when an operation targets a workstation id that isn't in the DB."""


class WorkstationConflictError(ValueError):
    """Raised when ``create_workstation`` is called with an existing id."""


def _flow_mode_to_columns(mode: FlowModeSpec | None) -> dict[str, Any]:
    """Flatten a FlowModeSpec into the seven DB columns. ``None`` for any
    field becomes NULL — Flow's UI is left untouched at runtime."""
    if mode is None:
        return {f"flow_mode_{k}": None for k in
                ("tab", "subtab", "aspect", "output_count", "duration_sec", "model")}
    return {
        "flow_mode_tab": mode.tab,
        "flow_mode_subtab": mode.subtab,
        "flow_mode_aspect": mode.aspect,
        "flow_mode_output_count": mode.output_count,
        "flow_mode_duration_sec": mode.duration_sec,
        "flow_mode_model": mode.model,
    }


def _flow_mode_from_row(row: sqlite3.Row) -> FlowModeSpec | None:
    fields = {
        "tab": row["flow_mode_tab"],
        "subtab": row["flow_mode_subtab"],
        "aspect": row["flow_mode_aspect"],
        "output_count": row["flow_mode_output_count"],
        "duration_sec": row["flow_mode_duration_sec"],
        "model": row["flow_mode_model"],
    }
    if all(v is None for v in fields.values()):
        return None
    return FlowModeSpec(**fields)


def _row_to_workstation(row: sqlite3.Row) -> WorkstationConfig:
    return WorkstationConfig(
        id=row["id"],
        account_label=row["account_label"],
        browser_profile_path=row["browser_profile_path"],
        daily_task_limit=row["daily_task_limit"],
        status=row["status"],
        flow_project_url=row["flow_project_url"],
        flow_mode=_flow_mode_from_row(row),
    )


def list_workstations(conn: sqlite3.Connection) -> list[WorkstationConfig]:
    """Return all workstations in deterministic id order."""
    rows = conn.execute(
        """
        SELECT id, account_label, browser_profile_path, daily_task_limit,
               status, flow_project_url, flow_mode_tab, flow_mode_subtab,
               flow_mode_aspect, flow_mode_output_count,
               flow_mode_duration_sec, flow_mode_model
          FROM workstations
         ORDER BY id ASC
        """
    ).fetchall()
    return [_row_to_workstation(r) for r in rows]


def get_workstation(conn: sqlite3.Connection, ws_id: str) -> WorkstationConfig | None:
    row = conn.execute(
        """
        SELECT id, account_label, browser_profile_path, daily_task_limit,
               status, flow_project_url, flow_mode_tab, flow_mode_subtab,
               flow_mode_aspect, flow_mode_output_count,
               flow_mode_duration_sec, flow_mode_model
          FROM workstations
         WHERE id = ?
        """,
        (ws_id,),
    ).fetchone()
    return _row_to_workstation(row) if row is not None else None


def create_workstation(conn: sqlite3.Connection, ws: WorkstationConfig) -> None:
    """Insert a brand-new workstation. Raises if id already exists.

    For the Web UI's "Add account" form. Use ``upsert_workstation`` from
    yaml-sync paths where re-running with the same id should be idempotent.
    """
    if get_workstation(conn, ws.id) is not None:
        raise WorkstationConflictError(f"workstation already exists: {ws.id}")
    flow_cols = _flow_mode_to_columns(ws.flow_mode)
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO workstations (
                id, account_label, browser_profile_path,
                daily_task_limit, status, flow_project_url,
                flow_mode_tab, flow_mode_subtab, flow_mode_aspect,
                flow_mode_output_count, flow_mode_duration_sec, flow_mode_model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ws.id, ws.account_label, ws.browser_profile_path,
                ws.daily_task_limit, ws.status, ws.flow_project_url,
                flow_cols["flow_mode_tab"], flow_cols["flow_mode_subtab"],
                flow_cols["flow_mode_aspect"], flow_cols["flow_mode_output_count"],
                flow_cols["flow_mode_duration_sec"], flow_cols["flow_mode_model"],
            ),
        )


def upsert_workstation(conn: sqlite3.Connection, ws: WorkstationConfig) -> str:
    """Insert if missing, otherwise update the editable subset (config-only —
    runtime ``status`` / cooldown / counters are preserved on update).

    Returns 'inserted' or 'updated' so callers can report what happened.
    """
    existing = get_workstation(conn, ws.id)
    flow_cols = _flow_mode_to_columns(ws.flow_mode)
    with transaction(conn):
        if existing is None:
            conn.execute(
                """
                INSERT INTO workstations (
                    id, account_label, browser_profile_path,
                    daily_task_limit, status, flow_project_url,
                    flow_mode_tab, flow_mode_subtab, flow_mode_aspect,
                    flow_mode_output_count, flow_mode_duration_sec, flow_mode_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ws.id, ws.account_label, ws.browser_profile_path,
                    ws.daily_task_limit, ws.status, ws.flow_project_url,
                    flow_cols["flow_mode_tab"], flow_cols["flow_mode_subtab"],
                    flow_cols["flow_mode_aspect"], flow_cols["flow_mode_output_count"],
                    flow_cols["flow_mode_duration_sec"], flow_cols["flow_mode_model"],
                ),
            )
            return "inserted"
        conn.execute(
            """
            UPDATE workstations SET
                account_label = ?,
                browser_profile_path = ?,
                daily_task_limit = ?,
                flow_project_url = ?,
                flow_mode_tab = ?,
                flow_mode_subtab = ?,
                flow_mode_aspect = ?,
                flow_mode_output_count = ?,
                flow_mode_duration_sec = ?,
                flow_mode_model = ?,
                updated_at = datetime('now')
             WHERE id = ?
            """,
            (
                ws.account_label, ws.browser_profile_path, ws.daily_task_limit,
                ws.flow_project_url,
                flow_cols["flow_mode_tab"], flow_cols["flow_mode_subtab"],
                flow_cols["flow_mode_aspect"], flow_cols["flow_mode_output_count"],
                flow_cols["flow_mode_duration_sec"], flow_cols["flow_mode_model"],
                ws.id,
            ),
        )
        return "updated"


def update_workstation_config(
    conn: sqlite3.Connection, ws_id: str, **fields: Any
) -> None:
    """Partial update for the Web UI's "Edit" form.

    Accepts any subset of ``_EDITABLE_COLUMNS`` plus the synthetic
    ``flow_mode`` (FlowModeSpec | None). Unknown keys raise ValueError so
    typos surface immediately rather than silently no-op'ing.
    """
    if get_workstation(conn, ws_id) is None:
        raise WorkstationNotFoundError(ws_id)

    sets: dict[str, Any] = {}
    if "flow_mode" in fields:
        sets.update(_flow_mode_to_columns(fields.pop("flow_mode")))

    for key, value in fields.items():
        if key not in _EDITABLE_COLUMNS:
            raise ValueError(f"field not editable: {key}")
        sets[key] = value
    if not sets:
        return

    set_sql = ", ".join(f"{col} = ?" for col in sets) + ", updated_at = datetime('now')"
    params = list(sets.values()) + [ws_id]
    with transaction(conn):
        conn.execute(f"UPDATE workstations SET {set_sql} WHERE id = ?", params)


def delete_workstation(conn: sqlite3.Connection, ws_id: str) -> bool:
    """Delete a workstation row. Returns True if a row was removed.

    Caller should detach any running tasks first — this DOES NOT cascade to
    ``tasks.assigned_workstation_id`` because that would silently break a
    real in-progress job. The Web UI must refuse delete when the WS has
    tasks in ``running``/``retry_waiting``.
    """
    with transaction(conn):
        cursor = conn.execute("DELETE FROM workstations WHERE id = ?", (ws_id,))
    return cursor.rowcount > 0

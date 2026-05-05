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

import logging
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app import paths as app_paths
from app.config.loader import FlowModeSpec, WorkstationConfig
from app.db.connection import transaction


_log = logging.getLogger("flow_harvester.workstations")


@dataclass
class WorkstationHealthHistory:
    """Aggregates from ``error_logs`` so the detail page can show
    "this account has been hit N times this week" — strike counter
    alone gets reset on hard revive / nurturing transition, so the
    raw numbers here are what actually expose long-running problem
    accounts.
    """
    unusual_activity_7d: int
    transitions_manual_check_24h: int
    revive_soft_24h: int
    revive_hard_24h: int


def get_workstation_health_history(
    conn: sqlite3.Connection, ws_id: str,
) -> WorkstationHealthHistory:
    def _count(error_types: tuple[str, ...], hours: int) -> int:
        placeholders = ", ".join("?" * len(error_types))
        params = [ws_id, *error_types, f"-{hours} hours"]
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n
              FROM error_logs
             WHERE workstation_id = ?
               AND error_type IN ({placeholders})
               AND created_at >= datetime('now', ?)
            """,
            params,
        ).fetchone()
        return int(row["n"] or 0)

    return WorkstationHealthHistory(
        unusual_activity_7d=_count(("unusual_activity",), 24 * 7),
        transitions_manual_check_24h=_count(("transition_manual_check",), 24),
        revive_soft_24h=_count(("revive_soft",), 24),
        revive_hard_24h=_count(("revive_hard",), 24),
    )


@dataclass
class WorkstationHealth:
    """Scheduler-owned runtime fields for a workstation. Read-only from the
    Web UI's perspective — the customer sees them on the detail page so
    they can tell at a glance how stressed each Google account is."""
    today_success_count: int
    today_failure_count: int
    consecutive_failure_count: int
    ban_probe_count: int
    cooldown_until: Optional[str]
    cooldown_reason: Optional[str]
    last_success_at: Optional[str]
    last_failure_at: Optional[str]


def _read_ban_probe_count(conn: sqlite3.Connection, ws_id: str) -> int:
    row = conn.execute(
        "SELECT ban_probe_count FROM workstations WHERE id = ?",
        (ws_id,),
    ).fetchone()
    return (row["ban_probe_count"] or 0) if row is not None else 0


def _write_health_audit(
    conn: sqlite3.Connection,
    ws_id: str,
    kind: str,
    prev_ban_probe_count: int,
) -> None:
    """Persist a one-line breadcrumb to ``error_logs`` whenever a health
    transition fires. Lets the WS detail page show "近 24h revive 次数"
    so the operator can spot accounts being reset over and over again.

    ``kind`` examples: ``revive_soft``, ``revive_hard``,
    ``transition_nurturing``, ``transition_disabled``.
    """
    conn.execute(
        """
        INSERT INTO error_logs
            (workstation_id, error_type, error_message, created_at)
        VALUES (?, ?, ?, datetime('now'))
        """,
        (ws_id, kind, f"prev_ban_probe_count={prev_ban_probe_count}"),
    )


def soft_revive_workstation(conn: sqlite3.Connection, ws_id: str) -> bool:
    """Login-callback path: a successful re-login proves the account is
    *reachable*, but says nothing about whether patchright will keep
    tripping Google's anti-bot. So we only clear the *transient* sticky
    state — cooldown timers, consecutive-failure count — and flip
    ``cooldown`` / ``busy`` rows back to ``healthy``.

    Critically: ``ban_probe_count`` is **not** touched, and rows in
    ``manual_check`` / ``nurturing`` / ``disabled`` keep their status.
    Those need an operator decision; a re-login alone shouldn't override
    that. The hard path below is the operator-explicit reset.
    """
    prev = _read_ban_probe_count(conn, ws_id)
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE workstations SET
                status = CASE
                    WHEN status IN ('cooldown', 'busy') THEN 'healthy'
                    ELSE status
                END,
                cooldown_until = NULL,
                cooldown_reason = NULL,
                consecutive_failure_count = 0,
                updated_at = datetime('now')
             WHERE id = ?
            """,
            (ws_id,),
        )
        if cursor.rowcount > 0:
            _write_health_audit(conn, ws_id, "revive_soft", prev)
    return cursor.rowcount > 0


def hard_revive_workstation(conn: sqlite3.Connection, ws_id: str) -> bool:
    """Operator-explicit "恢复使用" path: clears strikes + cooldown +
    flips status to ``healthy`` from any non-busy state. This is the
    only path that resets ``ban_probe_count``; success and re-login
    do not.
    """
    prev = _read_ban_probe_count(conn, ws_id)
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE workstations SET
                status = 'healthy',
                cooldown_until = NULL,
                cooldown_reason = NULL,
                consecutive_failure_count = 0,
                ban_probe_count = 0,
                updated_at = datetime('now')
             WHERE id = ?
            """,
            (ws_id,),
        )
        if cursor.rowcount > 0:
            _write_health_audit(conn, ws_id, "revive_hard", prev)
    return cursor.rowcount > 0


# Backwards-compatible alias — older callers / tests imported this name
# expecting the full reset semantics. Same behaviour as ``hard_revive``.
revive_workstation = hard_revive_workstation


def start_nurturing(conn: sqlite3.Connection, ws_id: str) -> bool:
    """Move a WS into ``nurturing``: the scheduler will not claim it
    (claim.py only takes ``healthy``), but the operator can keep using
    the same Chrome profile manually to rebuild the account's signal
    history. Strike counter is reset because the whole point of
    nurturing is to relieve patchright pressure.

    Allowed source states: ``healthy``, ``cooldown``, ``manual_check``.
    Refused for ``busy`` (job in flight) and ``disabled`` (use hard
    revive first).
    """
    prev = _read_ban_probe_count(conn, ws_id)
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE workstations SET
                status = 'nurturing',
                cooldown_until = NULL,
                cooldown_reason = NULL,
                consecutive_failure_count = 0,
                ban_probe_count = 0,
                updated_at = datetime('now')
             WHERE id = ?
               AND status IN ('healthy', 'cooldown', 'manual_check')
            """,
            (ws_id,),
        )
        if cursor.rowcount > 0:
            _write_health_audit(conn, ws_id, "transition_nurturing", prev)
    return cursor.rowcount > 0


def disable_workstation(conn: sqlite3.Connection, ws_id: str) -> bool:
    """Move a WS into ``disabled``: account considered dead / abandoned.
    Refused for ``busy`` to avoid yanking an in-flight job out from
    under the worker.
    """
    prev = _read_ban_probe_count(conn, ws_id)
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE workstations SET
                status = 'disabled',
                cooldown_until = NULL,
                cooldown_reason = NULL,
                updated_at = datetime('now')
             WHERE id = ?
               AND status != 'busy'
            """,
            (ws_id,),
        )
        if cursor.rowcount > 0:
            _write_health_audit(conn, ws_id, "transition_disabled", prev)
    return cursor.rowcount > 0


def get_workstation_health(
    conn: sqlite3.Connection, ws_id: str,
) -> Optional[WorkstationHealth]:
    """Return the scheduler runtime stats for a workstation, or None if
    the WS doesn't exist. Distinct from ``get_workstation`` which returns
    the user-editable config."""
    row = conn.execute(
        """
        SELECT today_success_count, today_failure_count,
               consecutive_failure_count, ban_probe_count,
               cooldown_until, cooldown_reason,
               last_success_at, last_failure_at
          FROM workstations WHERE id = ?
        """,
        (ws_id,),
    ).fetchone()
    if row is None:
        return None
    return WorkstationHealth(
        today_success_count=row["today_success_count"],
        today_failure_count=row["today_failure_count"],
        consecutive_failure_count=row["consecutive_failure_count"],
        ban_probe_count=row["ban_probe_count"] or 0,
        cooldown_until=row["cooldown_until"],
        cooldown_reason=row["cooldown_reason"],
        last_success_at=row["last_success_at"],
        last_failure_at=row["last_failure_at"],
    )


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


def _is_managed_profile(path: Path) -> bool:
    """True if ``path`` lives under the app's managed profiles dir.

    Guards against rm-rf'ing an arbitrary directory the operator may have
    pointed at via a custom ``browser_profile_path``. Only the customer-
    facing platform-default location (``paths.profiles_dir()``) qualifies.
    """
    try:
        managed = app_paths.profiles_dir().resolve()
        candidate = path.resolve()
    except (OSError, RuntimeError):
        return False
    if candidate == managed:
        return False
    return managed in candidate.parents


def delete_workstation(
    conn: sqlite3.Connection,
    ws_id: str,
    *,
    wipe_profile: bool = False,
) -> bool:
    """Delete a workstation row. Returns True if a row was removed.

    Caller should detach any running tasks first — this DOES NOT cascade to
    ``tasks.assigned_workstation_id`` because that would silently break a
    real in-progress job. The Web UI must refuse delete when the WS has
    tasks in ``running``/``retry_waiting``.

    When ``wipe_profile`` is True AND the workstation's
    ``browser_profile_path`` lives under ``paths.profiles_dir()``, the
    Chrome profile dir on disk is also removed. Profile paths outside the
    managed dir are left alone (with a warning log) so a custom path the
    operator typed in by hand is never deleted by accident.
    """
    profile_to_wipe: Path | None = None
    if wipe_profile:
        row = conn.execute(
            "SELECT browser_profile_path FROM workstations WHERE id = ?",
            (ws_id,),
        ).fetchone()
        if row is not None:
            candidate = Path(row["browser_profile_path"])
            if _is_managed_profile(candidate):
                profile_to_wipe = candidate
            else:
                _log.warning(
                    "skipping profile wipe for %s: %s is not under managed "
                    "profiles dir; remove it manually if needed",
                    ws_id, candidate,
                )

    with transaction(conn):
        cursor = conn.execute("DELETE FROM workstations WHERE id = ?", (ws_id,))
    deleted = cursor.rowcount > 0

    if deleted and profile_to_wipe is not None:
        shutil.rmtree(profile_to_wipe, ignore_errors=True)
    return deleted

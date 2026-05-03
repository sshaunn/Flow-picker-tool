"""Runtime app state — operator-toggled flags persisted in ``app_state``.

Currently just one knob: ``operation_mode`` (``day`` | ``night``). The
daemon reads it at the start of every pass so a customer toggling the
top-nav switch takes effect on the next idle-poll cycle without a
server restart.
"""

from __future__ import annotations

import sqlite3
from enum import Enum

from app.db.connection import transaction


class OperationMode(str, Enum):
    DAY = "day"
    NIGHT = "night"


_KEY = "operation_mode"


def get_operation_mode(conn: sqlite3.Connection) -> OperationMode:
    """Read the current mode. Defaults to DAY when unset (fresh install)
    so a customer who hasn't touched the toggle gets supervised behavior."""
    row = conn.execute(
        "SELECT value FROM app_state WHERE key = ?", (_KEY,),
    ).fetchone()
    if row is None:
        return OperationMode.DAY
    try:
        return OperationMode(row["value"])
    except ValueError:
        # Unknown value (manual DB edit / corrupted row) — fall back safe.
        return OperationMode.DAY


def set_operation_mode(
    conn: sqlite3.Connection, mode: OperationMode | str,
) -> OperationMode:
    """Persist the mode. Accepts the enum or its string value."""
    if isinstance(mode, str):
        mode = OperationMode(mode)
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO app_state (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (_KEY, mode.value),
        )
    return mode

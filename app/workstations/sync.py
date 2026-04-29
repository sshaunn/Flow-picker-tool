"""Sync workstation YAML config -> DB rows.

Idempotent: re-running with the same yaml just keeps the DB in sync. We
never overwrite the runtime state fields (``status``, ``today_*``,
``cooldown_until``) for an *existing* workstation — they belong to the
scheduler. We only update mutable config fields (``account_label``,
``browser_profile_path``, ``daily_task_limit``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from app.config.loader import WorkstationConfig
from app.db.connection import connect, transaction


def sync_workstations(db_path: Path | str, workstations: Iterable[WorkstationConfig]) -> tuple[int, int]:
    inserted = 0
    updated = 0
    conn = connect(db_path)
    try:
        with transaction(conn):
            for ws in workstations:
                row = conn.execute(
                    "SELECT id FROM workstations WHERE id = ?", (ws.id,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO workstations (
                            id, account_label, browser_profile_path,
                            daily_task_limit, status
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (ws.id, ws.account_label, ws.browser_profile_path, ws.daily_task_limit, ws.status),
                    )
                    inserted += 1
                else:
                    conn.execute(
                        """
                        UPDATE workstations
                           SET account_label = ?,
                               browser_profile_path = ?,
                               daily_task_limit = ?,
                               updated_at = datetime('now')
                         WHERE id = ?
                        """,
                        (ws.account_label, ws.browser_profile_path, ws.daily_task_limit, ws.id),
                    )
                    updated += 1
        return inserted, updated
    finally:
        conn.close()

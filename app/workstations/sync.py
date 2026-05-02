"""Sync workstation YAML config -> DB rows (legacy bootstrap path).

The DB is the source of truth for workstations; the Web UI mutates it
directly through ``app.workstations.repository``. This module now exists
only for two narrow cases:

* Initial bootstrap on a fresh DB — yaml is the operator's quickest way
  to populate the customer's workstation list before the Web UI is up.
* Dev convenience — ``run-once`` / ``run-worker`` still call this so a
  yaml edit is reflected on next run without a separate import step.

Behavior preserved from the previous implementation:
* Idempotent — re-running with the same yaml keeps the DB in sync.
* Runtime state fields (``status``, ``today_*``, ``cooldown_*``,
  ``ban_probe_count``, ``last_*_at``) are NEVER overwritten for an existing
  WS — they belong to the scheduler, not the config.
* All editable fields (now including ``flow_project_url`` + ``flow_mode_*``)
  are mirrored on update.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from app.config.loader import WorkstationConfig
from app.db.connection import connect
from app.workstations.repository import upsert_workstation


def sync_workstations(
    db_path: Path | str, workstations: Iterable[WorkstationConfig]
) -> tuple[int, int]:
    """Mirror the iterable of WorkstationConfig into the workstations table.

    Returns ``(inserted, updated)`` counts.
    """
    inserted = 0
    updated = 0
    conn = connect(db_path)
    try:
        for ws in workstations:
            outcome = upsert_workstation(conn, ws)
            if outcome == "inserted":
                inserted += 1
            else:
                updated += 1
    finally:
        conn.close()
    return inserted, updated

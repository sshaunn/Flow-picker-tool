"""Zombie task recovery (T17).

After a process crash, ``running`` rows whose ``started_at`` is older than
``running_stale_minutes`` are stuck. We:

1. Increment ``zombie_recovery_count``;
2. If ``zombie_recovery_count >= zombie_recovery_limit`` -> ``manual_review``;
3. Otherwise -> ``retry_waiting`` and ``retry_count += 1`` (per the
   ``running -> retry_waiting`` rule in T13);
4. Free any workstation that is still ``busy`` because of that task.

``task_results`` rows are *never* touched here — already-downloaded files
must persist (see docs/data-and-storage.md).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.config.loader import RecoverySettings
from app.db.connection import transaction


@dataclass
class RecoverySummary:
    revived: int
    escalated_manual: int


def recover_zombie_tasks(
    conn: sqlite3.Connection,
    *,
    cfg: RecoverySettings,
) -> RecoverySummary:
    threshold_clause = f"-{cfg.running_stale_minutes} minutes"
    revived = 0
    escalated = 0
    with transaction(conn):
        rows = conn.execute(
            """
            SELECT task_id, retry_count, max_retry_count,
                   zombie_recovery_count, assigned_workstation_id
              FROM tasks
             WHERE status = 'running'
               AND started_at IS NOT NULL
               AND started_at <= datetime('now', ?)
            """,
            (threshold_clause,),
        ).fetchall()

        for row in rows:
            task_id = row["task_id"]
            zombie_count = row["zombie_recovery_count"] + 1
            ws_id = row["assigned_workstation_id"]

            if zombie_count >= cfg.zombie_recovery_limit:
                conn.execute(
                    """
                    UPDATE tasks
                       SET status = 'manual_review',
                           zombie_recovery_count = ?,
                           error_type = 'internal',
                           error_message = 'zombie recovery limit reached'
                     WHERE task_id = ? AND status = 'running'
                    """,
                    (zombie_count, task_id),
                )
                escalated += 1
            else:
                # running -> retry_waiting -> retry_count += 1
                conn.execute(
                    """
                    UPDATE tasks
                       SET status = 'retry_waiting',
                           zombie_recovery_count = ?,
                           retry_count = retry_count + 1,
                           error_type = 'internal',
                           error_message = 'zombie running task recovered'
                     WHERE task_id = ? AND status = 'running'
                    """,
                    (zombie_count, task_id),
                )
                revived += 1

            if ws_id is not None:
                conn.execute(
                    "UPDATE workstations SET status = 'healthy', updated_at = datetime('now') "
                    "WHERE id = ? AND status = 'busy'",
                    (ws_id,),
                )

    return RecoverySummary(revived=revived, escalated_manual=escalated)


def reset_zombie_state_on_startup(conn: sqlite3.Connection) -> RecoverySummary:
    """Aggressive zombie recovery — call once per process boot.

    A fresh process has zero in-flight workers from the previous run by
    definition, so any task still in ``status='running'`` was orphaned
    when the previous process died (Ctrl+C, OOM, kill, crash, customer
    closing the cmd window). The mid-loop ``recover_zombie_tasks``
    requires the row to be ``running_stale_minutes`` old, which leaves
    the customer-facing UI stuck for several minutes after every
    restart — there's no central server they can hand-edit the DB on.

    This function bypasses the time threshold: every ``running`` task
    becomes ``retry_waiting`` (and bumps ``zombie_recovery_count`` so
    repeated crashes still escalate to ``manual_review``), and every
    workstation stuck ``busy`` returns to ``healthy``.

    Returns the same shape as ``recover_zombie_tasks`` so callers can
    log it the same way.
    """
    revived = 0
    escalated = 0
    with transaction(conn):
        rows = conn.execute(
            """
            SELECT task_id, retry_count, max_retry_count,
                   zombie_recovery_count, assigned_workstation_id
              FROM tasks
             WHERE status = 'running'
            """
        ).fetchall()

        for row in rows:
            task_id = row["task_id"]
            zombie_count = row["zombie_recovery_count"] + 1
            # Use the same escalation cap as the mid-loop recovery so a
            # task that keeps orphaning across restarts eventually goes
            # to manual_review instead of looping forever.
            limit = 3  # matches recovery.zombie_recovery_limit default
            if zombie_count >= limit:
                conn.execute(
                    """
                    UPDATE tasks
                       SET status = 'manual_review',
                           zombie_recovery_count = ?,
                           assigned_workstation_id = NULL,
                           started_at = NULL,
                           error_type = 'internal',
                           error_message = 'zombie recovery limit reached (restart cleanup)'
                     WHERE task_id = ? AND status = 'running'
                    """,
                    (zombie_count, task_id),
                )
                escalated += 1
            else:
                conn.execute(
                    """
                    UPDATE tasks
                       SET status = 'retry_waiting',
                           zombie_recovery_count = ?,
                           assigned_workstation_id = NULL,
                           started_at = NULL,
                           error_type = 'internal',
                           error_message = 'process restarted while task was running; resuming'
                     WHERE task_id = ? AND status = 'running'
                    """,
                    (zombie_count, task_id),
                )
                revived += 1

        # Free every busy workstation — without an in-flight worker
        # in this fresh process, ``busy`` is by definition stale.
        conn.execute(
            """
            UPDATE workstations
               SET status = 'healthy', updated_at = datetime('now')
             WHERE status = 'busy'
            """
        )

    return RecoverySummary(revived=revived, escalated_manual=escalated)

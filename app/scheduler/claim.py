"""Atomic task / workstation claim (T12).

The two operations — "pick a healthy workstation" and "pick a claimable task
and bind it to that workstation" — happen inside a single ``BEGIN
IMMEDIATE`` transaction (see docs/workflow-and-scheduling.md). If either
half fails, the transaction rolls back so the workstation never sticks in
``busy`` while the task table is unmodified.

Cross-day stats reset is the *first* step inside the same transaction so
that workstations whose ``stats_date`` is yesterday but counters are full
become eligible again immediately.

This module also exposes ``release_workstation`` for the runner to call when
no claimable task is found (so we don't strand a workstation in ``busy``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class ClaimResult:
    workstation_id: str
    task_row: dict


def _today_iso(today: Optional[date] = None) -> str:
    return (today or date.today()).isoformat()


def reset_stale_stats(conn: sqlite3.Connection, today: Optional[date] = None) -> int:
    """Reset today's counters for any workstation whose stats_date is stale.

    Safe to call inside an existing transaction.
    """
    today_str = _today_iso(today)
    cursor = conn.execute(
        """
        UPDATE workstations
           SET today_success_count = 0,
               today_failure_count = 0,
               consecutive_failure_count = 0,
               stats_date = ?
         WHERE stats_date IS NULL OR stats_date < ?
        """,
        (today_str, today_str),
    )
    return cursor.rowcount


def _pick_healthy_workstation(
    conn: sqlite3.Connection,
    today_str: str,
    only_workstation_id: Optional[str] = None,
) -> Optional[str]:
    # ``flow_project_url IS NOT NULL AND != ''``: a workstation that
    # was added but never had its project URL captured (login failed,
    # operator closed Chrome before capture, regex didn't match the
    # SPA's locale-prefixed URL, ...) has no usable project to point
    # the worker at. The worker would just navigate to the entry URL
    # and fail silently. Real customer bug, see diagnostic bundle of
    # 2026-05-08 12:51 — login a captured nothing because the SPA URL
    # included ``/zh/``, but the WS row stayed status='healthy' so
    # the scheduler kept claiming it for new tasks. Now: refuse to
    # claim until login produces a URL. Operator must complete login
    # (or paste a URL into the WS edit form) for the WS to become
    # eligible.
    sql = """
        SELECT id FROM workstations
         WHERE status = 'healthy'
           AND stats_date = ?
           AND (today_success_count + today_failure_count) < daily_task_limit
           AND flow_project_url IS NOT NULL
           AND flow_project_url != ''
    """
    params: list = [today_str]
    if only_workstation_id is not None:
        sql += " AND id = ?"
        params.append(only_workstation_id)
    sql += """
         ORDER BY (last_failure_at IS NULL) DESC,
                  last_failure_at ASC,
                  (last_success_at IS NULL) DESC,
                  last_success_at ASC,
                  id ASC
         LIMIT 1
    """
    row = conn.execute(sql, tuple(params)).fetchone()
    return row["id"] if row is not None else None


def _try_mark_busy(conn: sqlite3.Connection, ws_id: str) -> bool:
    cursor = conn.execute(
        "UPDATE workstations SET status = 'busy', updated_at = datetime('now') "
        "WHERE id = ? AND status = 'healthy'",
        (ws_id,),
    )
    return cursor.rowcount == 1


def _pick_claimable_task(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Find one claimable task. ``retry_waiting`` is preferred over ``pending``."""
    return conn.execute(
        """
        SELECT t.* FROM tasks AS t
         WHERE t.status IN ('pending', 'retry_waiting')
           AND t.retry_count < t.max_retry_count
           AND (
                t.depends_on_task_id IS NULL
             OR EXISTS (
                    SELECT 1 FROM tasks AS d
                     WHERE d.task_id = t.depends_on_task_id
                       AND d.status = 'success'
                )
           )
         ORDER BY (t.status = 'retry_waiting') DESC,
                  t.created_at ASC,
                  t.task_id ASC
         LIMIT 1
        """
    ).fetchone()


def _try_bind_task(
    conn: sqlite3.Connection,
    task_id: str,
    workstation_id: str,
    expected_status: str,
) -> Optional[sqlite3.Row]:
    cursor = conn.execute(
        """
        UPDATE tasks
           SET status = 'running',
               assigned_workstation_id = ?,
               started_at = datetime('now')
         WHERE task_id = ? AND status = ?
        """,
        (workstation_id, task_id, expected_status),
    )
    if cursor.rowcount != 1:
        return None
    return conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()


def claim_one(
    conn: sqlite3.Connection,
    *,
    today: Optional[date] = None,
    only_workstation_id: Optional[str] = None,
) -> Optional[ClaimResult]:
    """Atomically claim one workstation + task pair.

    Must be called *outside* an existing transaction; this function manages its
    own ``BEGIN IMMEDIATE`` so it can roll back cleanly. Returns ``None`` if no
    pair is available.
    """
    today_str = _today_iso(today)
    conn.execute("BEGIN IMMEDIATE")
    try:
        reset_stale_stats(conn, today)

        ws_id = _pick_healthy_workstation(conn, today_str, only_workstation_id)
        if ws_id is None:
            conn.execute("ROLLBACK")
            return None
        if not _try_mark_busy(conn, ws_id):
            # Lost the race to another scheduler.
            conn.execute("ROLLBACK")
            return None

        task_row = _pick_claimable_task(conn)
        if task_row is None:
            # No task to bind — roll back so the workstation stays healthy.
            conn.execute("ROLLBACK")
            return None

        bound = _try_bind_task(
            conn,
            task_id=task_row["task_id"],
            workstation_id=ws_id,
            expected_status=task_row["status"],
        )
        if bound is None:
            conn.execute("ROLLBACK")
            return None

        conn.execute("COMMIT")
        return ClaimResult(workstation_id=ws_id, task_row=dict(bound))
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def release_workstation(conn: sqlite3.Connection, workstation_id: str) -> None:
    """Force the workstation back to ``healthy`` (only if it was ``busy``)."""
    conn.execute(
        "UPDATE workstations SET status = 'healthy', updated_at = datetime('now') "
        "WHERE id = ? AND status = 'busy'",
        (workstation_id,),
    )

"""Task & workstation state transitions (T13, T14).

The runner owns a single function — ``finalize_task`` — that converts a
``WorkerOutcome`` into the right INSERT/UPDATE statements:

* ``running -> success``: task ``success``, workstation ``healthy`` + counters++.
* ``running -> retry_waiting``: ``retry_count += 1`` (only on this transition).
* ``running -> failed`` / ``download_failed``: ``retry_count`` unchanged.
* On the workstation side: ``manual_check`` traps for unusual_activity /
  login / captcha, ``cooldown`` for repeated normal failures or repeated
  page-load timeouts inside a configured window.

Cross-day reset & ``cooldown_until`` recovery are handled inside
``recover_workstation_states`` and inside the claim transaction.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config.loader import CooldownSettings


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    """Return an SQLite-compatible ``datetime('now')``-style string (UTC, no tz suffix)."""
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def finalize_task(
    conn: sqlite3.Connection,
    *,
    cooldown_cfg: CooldownSettings,
    task_id: str,
    workstation_id: str,
    final_status: str,
    downloaded_count: int,
    generation_round_count: int,
    last_error_type: Optional[str],
    last_error_message: Optional[str],
    workstation_outcome: str,
    result_folder: Optional[str],
) -> None:
    """Apply task & workstation state changes for a finished task.

    Must run inside a single transaction; this function expects the caller to
    open one (``transaction(conn)``).
    """
    if final_status not in {"success", "retry_waiting", "failed", "download_failed", "manual_review"}:
        raise ValueError(f"unexpected final_status: {final_status}")

    cur = conn.execute(
        "SELECT retry_count, status FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if cur is None:
        raise ValueError(f"task not found: {task_id}")
    prev_status = cur["status"]
    new_retry_count = cur["retry_count"]
    if final_status == "retry_waiting" and prev_status == "running":
        new_retry_count += 1

    conn.execute(
        """
        UPDATE tasks
           SET status = ?,
               downloaded_count = ?,
               generation_round_count = ?,
               retry_count = ?,
               error_type = ?,
               error_message = ?,
               result_folder = COALESCE(?, result_folder),
               finished_at = datetime('now')
         WHERE task_id = ?
        """,
        (
            final_status,
            downloaded_count,
            generation_round_count,
            new_retry_count,
            last_error_type,
            last_error_message,
            result_folder,
            task_id,
        ),
    )

    _apply_workstation_outcome(
        conn,
        cooldown_cfg=cooldown_cfg,
        workstation_id=workstation_id,
        final_status=final_status,
        workstation_outcome=workstation_outcome,
        last_error_type=last_error_type,
        last_error_message=last_error_message,
    )


def _apply_workstation_outcome(
    conn: sqlite3.Connection,
    *,
    cooldown_cfg: CooldownSettings,
    workstation_id: str,
    final_status: str,
    workstation_outcome: str,
    last_error_type: Optional[str],
    last_error_message: Optional[str],
) -> None:
    row = conn.execute(
        "SELECT * FROM workstations WHERE id = ?", (workstation_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"workstation not found: {workstation_id}")

    now = _utc_now()
    today_success = row["today_success_count"]
    today_failure = row["today_failure_count"]
    consecutive = row["consecutive_failure_count"]
    last_success = row["last_success_at"]
    last_failure = row["last_failure_at"]

    is_success = final_status == "success"
    if is_success:
        today_success += 1
        consecutive = 0
        last_success = _iso(now)
    else:
        today_failure += 1
        last_failure = _iso(now)
        if workstation_outcome == "page_failure":
            consecutive += 1
        elif workstation_outcome == "manual_check":
            # Manual-check errors don't necessarily indicate degrading account
            # health (e.g. login expired) — keep the counter where it was.
            pass
        elif final_status == "download_failed":
            # Generation succeeded but download broke — page health is fine.
            pass
        else:
            consecutive += 1

    new_status = row["status"]
    cooldown_until = row["cooldown_until"]
    cooldown_reason = row["cooldown_reason"]
    # Existing rows (pre-migration) may not yet have ban_probe_count;
    # treat NULL/missing as 0.
    try:
        ban_probe_count = row["ban_probe_count"] or 0
    except (KeyError, IndexError):
        ban_probe_count = 0
    # A successful generation proves the account is currently usable;
    # reset the strike counter so the next unusual_activity hit starts
    # from tier 0 again. (Without this, a single isolated strike would
    # haunt the WS forever.)
    if is_success:
        ban_probe_count = 0

    if workstation_outcome == "manual_check":
        cooldown_reason = last_error_type or "manual_check"
        # ``unusual_activity`` is observed to be intermittent — sticky on
        # the account but auto-clearing between generations. Burying the
        # WS in manual_check on the first hit is too aggressive: the
        # account often recovers within an hour. Use a strike budget:
        # the first ``max_strikes - 1`` hits put the WS in cooldown so
        # the next cron tick can let it retry; only after the last
        # strike do we fall through to operator-required manual_check.
        # ``login_required`` and ``captcha_or_verification`` go straight
        # to manual_check — those genuinely need a human.
        is_strike_eligible = last_error_type == "unusual_activity"
        strike_cooldowns = list(
            cooldown_cfg.unusual_activity_strike_cooldown_minutes or []
        )
        max_strikes = cooldown_cfg.unusual_activity_max_strikes
        if is_strike_eligible and (ban_probe_count + 1) < max_strikes:
            ban_probe_count += 1
            tier = min(ban_probe_count - 1, len(strike_cooldowns) - 1)
            tier_minutes = strike_cooldowns[tier] if strike_cooldowns else 30
            new_status = "cooldown"
            cooldown_until = _iso(now + timedelta(minutes=tier_minutes))
            cooldown_reason = (
                f"unusual_activity_strike_{ban_probe_count}"
            )
        else:
            new_status = "manual_check"
            # Schedule a probe-based recovery anchor at tier-0 of the
            # probe backoff so ``probe_recover_banned_workstations`` can
            # later flip the WS back if the page actually clears.
            backoff = list(cooldown_cfg.unusual_activity_probe_backoff_hours or [])
            if backoff:
                cooldown_until = _iso(now + timedelta(hours=backoff[0]))
            else:
                cooldown_until = None
            # Reset strike counter on entering manual_check; the probe
            # recovery has its own counter semantics.
            ban_probe_count = 0
    elif workstation_outcome == "page_failure":
        if consecutive >= cooldown_cfg.consecutive_failure_threshold:
            new_status = "cooldown"
            cooldown_until = _iso(
                now + timedelta(minutes=cooldown_cfg.cooldown_duration_short_min)
            )
            cooldown_reason = "consecutive_failure"
        elif _recent_page_failures(
            conn,
            workstation_id=workstation_id,
            window_minutes=cooldown_cfg.page_failure_window_min,
        ) >= cooldown_cfg.page_failure_threshold:
            new_status = "cooldown"
            cooldown_until = _iso(
                now + timedelta(minutes=cooldown_cfg.cooldown_duration_long_min)
            )
            cooldown_reason = "page_failure_window"
        else:
            new_status = "healthy"
    else:
        new_status = "healthy"
        cooldown_until = None
        cooldown_reason = None

    conn.execute(
        """
        UPDATE workstations
           SET status = ?,
               today_success_count = ?,
               today_failure_count = ?,
               consecutive_failure_count = ?,
               last_success_at = ?,
               last_failure_at = ?,
               cooldown_until = ?,
               cooldown_reason = ?,
               ban_probe_count = ?,
               updated_at = datetime('now')
         WHERE id = ?
        """,
        (
            new_status,
            today_success,
            today_failure,
            consecutive,
            last_success,
            last_failure,
            cooldown_until,
            cooldown_reason,
            ban_probe_count,
            workstation_id,
        ),
    )


def _recent_page_failures(
    conn: sqlite3.Connection,
    *,
    workstation_id: str,
    window_minutes: int,
) -> int:
    cursor = conn.execute(
        """
        SELECT COUNT(*) AS n FROM error_logs
         WHERE workstation_id = ?
           AND error_type IN ('page_load_failed', 'timeout')
           AND created_at >= datetime('now', ?)
        """,
        (workstation_id, f'-{window_minutes} minutes'),
    )
    return int(cursor.fetchone()["n"])


def recover_workstation_states(conn: sqlite3.Connection) -> int:
    """Move ``cooldown`` workstations whose ``cooldown_until`` elapsed back to
    ``healthy``. Returns the number of rows touched.
    """
    cursor = conn.execute(
        """
        UPDATE workstations
           SET status = 'healthy',
               consecutive_failure_count = 0,
               cooldown_until = NULL,
               cooldown_reason = NULL,
               updated_at = datetime('now')
         WHERE status = 'cooldown'
           AND cooldown_until IS NOT NULL
           AND cooldown_until <= datetime('now')
        """
    )
    return cursor.rowcount


def probe_recover_banned_workstations(
    conn: sqlite3.Connection,
    *,
    cooldown_cfg: CooldownSettings,
    probe_fn,
) -> dict:
    """Probe ``manual_check`` workstations whose ``cooldown_until`` elapsed
    and either flip them back to ``healthy`` or push to the next backoff
    tier. Returns ``{"recovered": N, "still_banned": N, "exhausted": N}``.

    ``probe_fn(workstation_id) -> bool`` must return True if the account is
    *still* banned (probe page still shows unusual_activity / captcha),
    False if it is clean enough to start work again. The probe is
    injected so unit tests can substitute a deterministic stub instead
    of launching Chromium.
    """
    rows = conn.execute(
        """
        SELECT id, ban_probe_count, cooldown_reason
          FROM workstations
         WHERE status = 'manual_check'
           AND cooldown_until IS NOT NULL
           AND cooldown_until <= datetime('now')
        """
    ).fetchall()
    backoff = list(cooldown_cfg.unusual_activity_probe_backoff_hours or [])
    stats = {"recovered": 0, "still_banned": 0, "exhausted": 0}
    now = _utc_now()
    for row in rows:
        ws_id = row["id"]
        prev_count = row["ban_probe_count"] or 0
        try:
            still_banned = bool(probe_fn(ws_id))
        except Exception:
            # Probe itself crashed (network, Playwright crash). Don't
            # flip status; just leave cooldown_until in place — caller
            # will retry next cron tick.
            continue
        if not still_banned:
            conn.execute(
                """
                UPDATE workstations
                   SET status = 'healthy',
                       consecutive_failure_count = 0,
                       cooldown_until = NULL,
                       cooldown_reason = NULL,
                       ban_probe_count = 0,
                       updated_at = datetime('now')
                 WHERE id = ?
                """,
                (ws_id,),
            )
            stats["recovered"] += 1
            continue
        next_count = prev_count + 1
        if next_count >= len(backoff):
            # Exhausted backoff tiers — keep in manual_check, drop
            # cooldown_until so the recovery loop stops touching it.
            # An operator must intervene at this point.
            conn.execute(
                """
                UPDATE workstations
                   SET cooldown_until = NULL,
                       cooldown_reason = COALESCE(cooldown_reason, '') || ':exhausted',
                       ban_probe_count = ?,
                       updated_at = datetime('now')
                 WHERE id = ?
                """,
                (next_count, ws_id),
            )
            stats["exhausted"] += 1
        else:
            conn.execute(
                """
                UPDATE workstations
                   SET cooldown_until = ?,
                       ban_probe_count = ?,
                       updated_at = datetime('now')
                 WHERE id = ?
                """,
                (
                    _iso(now + timedelta(hours=backoff[next_count])),
                    next_count,
                    ws_id,
                ),
            )
            stats["still_banned"] += 1
    return stats


def release_orphaned_busy_workstations(conn: sqlite3.Connection) -> int:
    """Flip any workstation stuck in ``busy`` with no ``running`` task back
    to ``healthy``.

    This happens when a runner is killed (Ctrl+C, OOM, etc.) between the
    atomic claim that sets ``busy`` and the ``finalize_task`` call that
    would release it. Without this recovery the workstation is unclaimable
    until an operator hand-edits the DB.

    Note this is independent of zombie *task* recovery (T17): a task can be
    legitimately ``running`` for up to ``running_stale_minutes`` before
    recovery kicks in, but if the assigned workstation is ``busy`` while
    the task is *not* in ``running`` state, the workstation is definitely
    orphaned.
    """
    cursor = conn.execute(
        """
        UPDATE workstations
           SET status = 'healthy',
               updated_at = datetime('now')
         WHERE status = 'busy'
           AND id NOT IN (
                SELECT assigned_workstation_id FROM tasks
                 WHERE status = 'running'
                   AND assigned_workstation_id IS NOT NULL
           )
        """
    )
    return cursor.rowcount


def force_manual_check(
    conn: sqlite3.Connection, *, workstation_id: str, reason: str
) -> None:
    conn.execute(
        """
        UPDATE workstations
           SET status = 'manual_check',
               cooldown_until = NULL,
               cooldown_reason = ?,
               updated_at = datetime('now')
         WHERE id = ?
        """,
        (reason, workstation_id),
    )

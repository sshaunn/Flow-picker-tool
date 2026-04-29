"""Error capture & screenshot tooling (T05).

``save_error_snapshot`` is the single entry point Worker code uses to capture
both a screenshot (best-effort — saving the image must never swallow the
underlying exception) and an ``error_logs`` row including the
``generation_round`` so we can trace which round failed.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from app.utils.paths import screenshot_filename, screenshots_dir


@dataclass
class ErrorSnapshot:
    error_log_id: int
    screenshot_path: Optional[str]


# Error type vocabulary, keep aligned with docs/workflow-and-scheduling.md.
ERROR_TYPES = (
    "unusual_activity",
    "login_required",
    "captcha_or_verification",
    "page_load_failed",
    "service_unavailable",
    "generation_failed",
    "download_failed",
    "timeout",
    "profile_unavailable",
    "internal",
)


def _take_screenshot(
    take_screenshot_fn: Optional[Callable[[Path], None]],
    target_path: Path,
    log: logging.Logger,
) -> Optional[Path]:
    if take_screenshot_fn is None:
        return None
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        take_screenshot_fn(target_path)
        if target_path.exists():
            return target_path
        log.warning("screenshot fn returned but file missing: %s", target_path)
        return None
    except Exception as exc:  # noqa: BLE001 - never swallow original error path
        log.warning("screenshot capture failed: %s", exc)
        return None


def save_error_snapshot(
    conn: sqlite3.Connection,
    *,
    log: logging.Logger,
    task_id: Optional[str],
    workstation_id: Optional[str],
    generation_round: Optional[int],
    error_type: str,
    error_message: str,
    segment_dir: Optional[Path] = None,
    take_screenshot_fn: Optional[Callable[[Path], None]] = None,
) -> ErrorSnapshot:
    """Persist a screenshot (best-effort) and write an ``error_logs`` row.

    Even if screenshot capture fails, we still write the error log row so
    debugging is possible. The original failure context is preserved by
    callers (this function does not re-raise on screenshot failures).
    """
    if error_type not in ERROR_TYPES:
        log.warning("unknown error_type=%r — recording anyway", error_type)

    screenshot_path: Optional[Path] = None
    if segment_dir is not None and task_id is not None and generation_round is not None:
        target = screenshots_dir(segment_dir) / screenshot_filename(
            task_id, generation_round, error_type
        )
        screenshot_path = _take_screenshot(take_screenshot_fn, target, log)

    cursor = conn.execute(
        """
        INSERT INTO error_logs (
            task_id, workstation_id, generation_round,
            error_type, error_message, screenshot_path
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            workstation_id,
            generation_round,
            error_type,
            error_message,
            str(screenshot_path) if screenshot_path else None,
        ),
    )
    log.error(
        "error_type=%s task_id=%s ws=%s round=%s msg=%s screenshot=%s",
        error_type,
        task_id,
        workstation_id,
        generation_round,
        error_message,
        screenshot_path or "<none>",
    )
    return ErrorSnapshot(
        error_log_id=int(cursor.lastrowid or 0),
        screenshot_path=str(screenshot_path) if screenshot_path else None,
    )


def fmt_exc(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _ignored(*_: Any) -> None:  # pragma: no cover - placeholder hook
    return None

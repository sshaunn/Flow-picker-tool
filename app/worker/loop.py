"""Candidate generation loop (T08, T09, T10).

Drives a single Segment task to completion. The Worker:

1. Calls ``flow.open()`` and classifies the page state.
2. Enters a round loop, where each round:
   a. Increments ``generation_round_count``.
   b. Uploads source asset, pastes prompt, triggers generation.
   c. Waits for the round to complete.
   d. Downloads each candidate; writes a ``task_results`` row per candidate.
3. Exits the loop when:
   * ``downloaded_count >= target_count`` -> ``success``,
   * the page returns a circuit-breaker state -> ``retry_waiting``,
   * the round produced candidates but **none** could be downloaded ->
     ``download_failed`` (avoid burning Flow quota by re-running generation),
   * ``generation_round_count >= max_round`` -> ``failed`` /
     ``retry_waiting`` based on how much was downloaded.

The Worker does NOT update the workstation status here — the runner owns
that policy (T14). It returns a ``WorkerOutcome`` with everything the runner
needs to flip task & workstation state in one place.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from app.config.loader import GenerationSettings
from app.utils.errors import fmt_exc, save_error_snapshot
from app.utils.paths import (
    candidate_extension,
    ensure_segment_layout,
    screenshots_dir,
    video_filename,
)
from app.worker.flow_port import (
    CandidateMeta,
    FlowPort,
    FlowPortError,
    GenerationRoundResult,
    PageState,
    SourceAsset,
)


# Outcome the runner uses to update task/workstation rows.

CIRCUIT_BREAKER_STATES: frozenset[PageState] = frozenset({
    PageState.UNUSUAL_ACTIVITY,
    PageState.LOGIN_REQUIRED,
    PageState.CAPTCHA_OR_VERIFICATION,
})

# Page states that are transient / page-level (not workstation health issues).
# Treat them as ``page_failure`` so the workstation accumulates cooldown
# pressure but isn't yanked into manual_check.
PAGE_FAILURE_STATES: frozenset[PageState] = frozenset({
    PageState.PAGE_LOAD_FAILED,
    PageState.SERVICE_UNAVAILABLE,
})

# Page states that should map directly to error_logs.error_type.
_PAGE_STATE_TO_ERROR_TYPE = {
    PageState.UNUSUAL_ACTIVITY: "unusual_activity",
    PageState.LOGIN_REQUIRED: "login_required",
    PageState.CAPTCHA_OR_VERIFICATION: "captcha_or_verification",
    PageState.PAGE_LOAD_FAILED: "page_load_failed",
    PageState.SERVICE_UNAVAILABLE: "service_unavailable",
}


@dataclass
class TaskInput:
    task_id: str
    sku_id: str
    creative_id: str
    segment_id: str
    source_asset_path: Path  # legacy: the first asset, kept for back-compat
    video_prompt: str
    target_count: int
    initial_downloaded_count: int = 0
    initial_round_count: int = 0
    # Full ordered asset list. If empty, the worker falls back to
    # ``[SourceAsset(path=source_asset_path, kind='first_frame', order=1)]``.
    source_assets: list[SourceAsset] = field(default_factory=list)


@dataclass
class WorkerOutcome:
    """Final state to commit for a single task run."""

    final_status: str  # 'success' | 'retry_waiting' | 'failed' | 'download_failed'
    downloaded_count: int
    generation_round_count: int
    last_error_type: str | None
    last_error_message: str | None
    workstation_outcome: str  # 'healthy' | 'manual_check' | 'cooldown_signal' | 'page_failure'
    candidates_persisted: list[dict]
    result_folder: str


def execute_task(
    *,
    conn: sqlite3.Connection,
    log: logging.Logger,
    flow: FlowPort,
    workstation_id: str,
    task: TaskInput,
    config: GenerationSettings,
    output_root: Path,
    run_date: date,
    captcha_action: str = "pause",
    inter_round_pause_sec: int | None = None,
) -> WorkerOutcome:
    seg_dir = ensure_segment_layout(
        output_root=output_root,
        run_date=run_date,
        sku_id=task.sku_id,
        creative_id=task.creative_id,
        segment_id=task.segment_id,
    )
    log.info(
        "task start task_id=%s ws=%s creative=%s segment=%s target=%d",
        task.task_id, workstation_id, task.creative_id, task.segment_id, task.target_count,
    )

    downloaded_count = task.initial_downloaded_count
    # ``round_count`` is the storage cursor — it advances strictly
    # forward across resumes so generated mp4s land in fresh
    # ``task_results`` rows (UNIQUE on task_id, generation_round,
    # sequence_no). ``session_round_count`` is the per-call budget
    # against ``max_round_per_task`` so a resumed task gets a full
    # ``max_round`` window of fresh attempts instead of immediately
    # tripping the cap left over from the previous session.
    #
    # Authoritative cursor source is the actual MAX(generation_round)
    # in ``task_results`` — defends against operator edits to the
    # ``tasks.generation_round_count`` column that would otherwise let
    # the worker write rounds 1..N already used and silently skip on
    # the UNIQUE collision.
    cursor_row = conn.execute(
        "SELECT COALESCE(MAX(generation_round), 0) AS max_round "
        "FROM task_results WHERE task_id = ?",
        (task.task_id,),
    ).fetchone()
    persisted_max = int(cursor_row["max_round"] if cursor_row else 0)
    round_count = max(task.initial_round_count, persisted_max)
    session_round_count = 0
    last_error_type: str | None = None
    last_error_message: str | None = None
    candidates_persisted: list[dict] = []
    workstation_outcome = "healthy"
    final_status = "retry_waiting"

    try:
        page_state = flow.open()
        if page_state in CIRCUIT_BREAKER_STATES:
            error_type = _PAGE_STATE_TO_ERROR_TYPE[page_state]
            save_error_snapshot(
                conn,
                log=log,
                task_id=task.task_id,
                workstation_id=workstation_id,
                generation_round=None,
                error_type=error_type,
                error_message=f"page state on open: {page_state.value}",
                segment_dir=seg_dir,
                take_screenshot_fn=flow.take_screenshot,
            )
            return WorkerOutcome(
                final_status="retry_waiting",
                downloaded_count=downloaded_count,
                generation_round_count=round_count,
                last_error_type=error_type,
                last_error_message=f"page state on open: {page_state.value}",
                workstation_outcome="manual_check",
                candidates_persisted=candidates_persisted,
                result_folder=str(seg_dir),
            )
        if page_state in PAGE_FAILURE_STATES:
            error_type = _PAGE_STATE_TO_ERROR_TYPE[page_state]
            error_message = (
                "Flow service unavailable on open"
                if page_state == PageState.SERVICE_UNAVAILABLE
                else "page failed to load on open"
            )
            save_error_snapshot(
                conn,
                log=log,
                task_id=task.task_id,
                workstation_id=workstation_id,
                generation_round=None,
                error_type=error_type,
                error_message=error_message,
                segment_dir=seg_dir,
                take_screenshot_fn=flow.take_screenshot,
            )
            return WorkerOutcome(
                final_status="retry_waiting",
                downloaded_count=downloaded_count,
                generation_round_count=round_count,
                last_error_type=error_type,
                last_error_message=error_message,
                workstation_outcome="page_failure",
                candidates_persisted=candidates_persisted,
                result_folder=str(seg_dir),
            )

        while downloaded_count < task.target_count:
            if session_round_count >= config.max_round_per_task:
                log.info(
                    "task task_id=%s reached max_round=%d (session), exiting loop",
                    task.task_id, config.max_round_per_task,
                )
                break

            # Pause between consecutive rounds to spread requests so
            # Google's per-account rate limiter (the trigger behind
            # unusual_activity) is less likely to fire. Skip on round 1
            # — there's no prior round to space out against. Day / night
            # profile may override the per-mode wait via
            # ``inter_round_pause_sec``; otherwise use the global default.
            pause_sec = (
                inter_round_pause_sec if inter_round_pause_sec is not None
                else getattr(config, "inter_round_pause_sec", 0)
            )
            if round_count >= 1 and pause_sec > 0:
                time.sleep(pause_sec)

            round_count += 1
            session_round_count += 1
            log.info(
                "round start task_id=%s round=%d downloaded=%d/%d",
                task.task_id, round_count, downloaded_count, task.target_count,
            )

            assets = task.source_assets or [
                SourceAsset(path=task.source_asset_path, kind="first_frame", order=1)
            ]
            try:
                flow.upload_source_assets(assets)
                flow.paste_prompt(task.video_prompt)
                flow.trigger_generation()
            except FlowPortError as exc:
                last_error_type = "generation_failed"
                last_error_message = fmt_exc(exc)
                save_error_snapshot(
                    conn,
                    log=log,
                    task_id=task.task_id,
                    workstation_id=workstation_id,
                    generation_round=round_count,
                    error_type=last_error_type,
                    error_message=last_error_message,
                    segment_dir=seg_dir,
                    take_screenshot_fn=flow.take_screenshot,
                )
                workstation_outcome = "page_failure"
                break

            try:
                round_result = flow.wait_for_round_complete(
                    timeout_sec=config.generation_wait_timeout_sec
                )
            except FlowPortError as exc:
                last_error_type = "timeout"
                last_error_message = fmt_exc(exc)
                save_error_snapshot(
                    conn,
                    log=log,
                    task_id=task.task_id,
                    workstation_id=workstation_id,
                    generation_round=round_count,
                    error_type=last_error_type,
                    error_message=last_error_message,
                    segment_dir=seg_dir,
                    take_screenshot_fn=flow.take_screenshot,
                )
                workstation_outcome = "page_failure"
                break

            if round_result.state in CIRCUIT_BREAKER_STATES:
                last_error_type = _PAGE_STATE_TO_ERROR_TYPE[round_result.state]
                last_error_message = round_result.error_message or round_result.state.value
                save_error_snapshot(
                    conn,
                    log=log,
                    task_id=task.task_id,
                    workstation_id=workstation_id,
                    generation_round=round_count,
                    error_type=last_error_type,
                    error_message=last_error_message,
                    segment_dir=seg_dir,
                    take_screenshot_fn=flow.take_screenshot,
                )
                workstation_outcome = "manual_check"
                break

            if round_result.state in PAGE_FAILURE_STATES or round_result.timed_out:
                if round_result.timed_out:
                    last_error_type = "timeout"
                else:
                    last_error_type = _PAGE_STATE_TO_ERROR_TYPE[round_result.state]
                last_error_message = round_result.error_message or last_error_type
                save_error_snapshot(
                    conn,
                    log=log,
                    task_id=task.task_id,
                    workstation_id=workstation_id,
                    generation_round=round_count,
                    error_type=last_error_type,
                    error_message=last_error_message,
                    segment_dir=seg_dir,
                    take_screenshot_fn=flow.take_screenshot,
                )
                workstation_outcome = "page_failure"
                break

            round_persisted, round_failed = _download_round(
                conn=conn,
                log=log,
                flow=flow,
                seg_dir=seg_dir,
                workstation_id=workstation_id,
                task=task,
                round_index=round_count,
                round_result=round_result,
            )
            downloaded_count += len(round_persisted)
            candidates_persisted.extend(round_persisted)

            if not round_persisted and round_failed > 0:
                # Generation happened, but no candidate could be downloaded:
                # do NOT re-trigger generation (avoid burning Flow quota).
                last_error_type = "download_failed"
                last_error_message = (
                    f"round {round_count}: all {round_failed} candidate(s) failed to download"
                )
                final_status = "download_failed"
                workstation_outcome = "healthy"
                break

            if not round_result.candidates:
                # generation produced 0 candidates (Flow may have rejected silently)
                last_error_type = "generation_failed"
                last_error_message = (
                    f"round {round_count}: 0 candidates produced"
                )
                save_error_snapshot(
                    conn,
                    log=log,
                    task_id=task.task_id,
                    workstation_id=workstation_id,
                    generation_round=round_count,
                    error_type=last_error_type,
                    error_message=last_error_message,
                    segment_dir=seg_dir,
                    take_screenshot_fn=flow.take_screenshot,
                )

        # Loop exit -> resolve final status.
        if final_status != "download_failed":
            if downloaded_count >= task.target_count:
                final_status = "success"
                workstation_outcome = "healthy"
            elif workstation_outcome == "manual_check":
                # Day mode: stay in retry_waiting so the operator can act
                # (login expired / captcha) and the same task picks up
                # again. Night mode: there is no operator — flip the task
                # to manual_review so it shows up next morning instead of
                # blocking the queue while the WS is parked.
                if captcha_action == "skip" and last_error_type in {
                    "captcha_or_verification", "login_required",
                }:
                    final_status = "manual_review"
                else:
                    final_status = "retry_waiting"
            elif workstation_outcome == "page_failure":
                final_status = "retry_waiting"
            elif session_round_count >= config.max_round_per_task:
                final_status = "failed"
            else:
                final_status = "retry_waiting"

    finally:
        try:
            flow.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("flow.close() raised: %s", exc)

    log.info(
        "task end task_id=%s status=%s downloaded=%d/%d round=%d ws_outcome=%s",
        task.task_id, final_status, downloaded_count, task.target_count, round_count,
        workstation_outcome,
    )

    return WorkerOutcome(
        final_status=final_status,
        downloaded_count=downloaded_count,
        generation_round_count=round_count,
        last_error_type=last_error_type,
        last_error_message=last_error_message,
        workstation_outcome=workstation_outcome,
        candidates_persisted=candidates_persisted,
        result_folder=str(seg_dir),
    )


def _download_round(
    *,
    conn: sqlite3.Connection,
    log: logging.Logger,
    flow: FlowPort,
    seg_dir: Path,
    workstation_id: str,
    task: TaskInput,
    round_index: int,
    round_result: GenerationRoundResult,
) -> tuple[list[dict], int]:
    """Download every candidate this round produced. Returns (persisted, failed)."""
    persisted: list[dict] = []
    failed = 0
    if not round_result.candidates:
        return persisted, failed

    for candidate in round_result.candidates:
        ext = candidate_extension(candidate.media_kind)
        target_path = seg_dir / video_filename(
            task.task_id, round_index, candidate.sequence_no, ext
        )
        try:
            flow.download_candidate(candidate, target_path)
        except FlowPortError as exc:
            failed += 1
            save_error_snapshot(
                conn,
                log=log,
                task_id=task.task_id,
                workstation_id=workstation_id,
                generation_round=round_index,
                error_type="download_failed",
                error_message=fmt_exc(exc),
                segment_dir=seg_dir,
                take_screenshot_fn=flow.take_screenshot,
            )
            continue
        try:
            conn.execute(
                """
                INSERT INTO task_results (
                    task_id, creative_id, segment_id, workstation_id,
                    generation_round, sequence_no, video_file_path,
                    screenshot_path, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'downloaded')
                """,
                (
                    task.task_id,
                    task.creative_id,
                    task.segment_id,
                    workstation_id,
                    round_index,
                    candidate.sequence_no,
                    str(target_path),
                    None,
                ),
            )
        except sqlite3.IntegrityError:
            # Recovery scenario: same (task, round, seq) already on disk.
            log.info(
                "task_results already exists task_id=%s round=%d seq=%d — skipping",
                task.task_id, round_index, candidate.sequence_no,
            )
            continue
        # Bump live progress counters so the dashboard's WebSocket push
        # reflects each downloaded candidate as it lands, instead of
        # appearing stuck at the round-start value until finalize_task.
        conn.execute(
            """
            UPDATE tasks SET
                downloaded_count = (
                    SELECT COUNT(*) FROM task_results WHERE task_id = ?
                ),
                generation_round_count = MAX(generation_round_count, ?)
            WHERE task_id = ?
            """,
            (task.task_id, round_index, task.task_id),
        )
        persisted.append(
            {
                "task_id": task.task_id,
                "round": round_index,
                "sequence_no": candidate.sequence_no,
                "video_file_path": str(target_path),
            }
        )
    return persisted, failed

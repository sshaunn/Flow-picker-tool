"""Multi-workstation runner (T15).

The scheduler loop runs on the main thread. Each scheduling round:

1. Recovers cooldown timers and zombie ``running`` rows.
2. Atomically claims one (workstation, task) pair (T12).
3. Submits the run to a thread pool keyed by workstation id, so each
   workstation only runs one task at a time.
4. Loops until either no claim succeeds for a full pass *or* ``max_rounds``
   is reached.

Workstation isolation: a workstation that ends up in ``manual_check`` /
``cooldown`` / ``disabled`` is not claimed again — the others keep going.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from app.config.loader import AppConfig, WorkstationConfig
from app.db.connection import connect, transaction
from app.scheduler.claim import claim_one
from app.scheduler.recovery import recover_zombie_tasks
from app.scheduler.state import (
    finalize_task,
    recover_workstation_states,
    release_orphaned_busy_workstations,
)
from app.utils.logging import get_scheduler_logger, get_worker_logger
from app.workstations.profile_check import check_profile
from app.workstations.sync import sync_workstations
from app.worker.flow_mock import MockFlowPort, MockRoundPlan
from app.worker.flow_port import FlowPort, PageState, SourceAsset
from app.worker.loop import TaskInput, execute_task


def _load_source_assets(conn, task_id: str) -> list[SourceAsset]:
    rows = conn.execute(
        "SELECT asset_order, asset_path, asset_type FROM task_assets "
        "WHERE task_id = ? ORDER BY asset_order ASC",
        (task_id,),
    ).fetchall()
    return [
        SourceAsset(path=Path(r["asset_path"]), kind=r["asset_type"], order=r["asset_order"])
        for r in rows
    ]


@dataclass
class MultiRunSummary:
    executed: int = 0
    success: int = 0
    failed: int = 0
    download_failed: int = 0
    retry_waiting: int = 0
    manual_review: int = 0


def _build_flow_port(
    config: AppConfig,
    workstation: WorkstationConfig,
    *,
    use_mock: bool,
    mock_round_plans: Optional[list[MockRoundPlan]],
    mock_initial_state: PageState,
) -> FlowPort:
    if use_mock:
        plans = mock_round_plans if mock_round_plans is not None else [MockRoundPlan.success(4)]
        return MockFlowPort(round_plans=plans, initial_state=mock_initial_state)
    try:
        from app.worker.flow_playwright import PlaywrightFlowPort  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Playwright FlowPort not available. Install playwright or use --mock."
        ) from exc
    return PlaywrightFlowPort(
        entry_url=config.flow.entry_url,
        profile_path=Path(workstation.browser_profile_path),
        page_action_timeout_sec=config.generation.page_action_timeout_sec,
        project_url=workstation.flow_project_url,
        flow_mode_spec=workstation.flow_mode,
    )


def _execute_in_thread(
    *,
    db_path: Path,
    config: AppConfig,
    workstation: WorkstationConfig,
    task_row: dict,
    use_mock: bool,
    mock_round_plans: Optional[list[MockRoundPlan]],
    mock_initial_state: PageState,
    run_date: date,
) -> str:
    log = get_worker_logger(config.log_root, workstation.id)
    flow = _build_flow_port(
        config,
        workstation,
        use_mock=use_mock,
        mock_round_plans=mock_round_plans,
        mock_initial_state=mock_initial_state,
    )
    conn = connect(db_path)
    try:
        task = TaskInput(
            task_id=task_row["task_id"],
            sku_id=task_row["sku_id"],
            creative_id=task_row["creative_id"],
            segment_id=task_row["segment_id"],
            source_asset_path=Path(task_row["source_asset_path"]),
            video_prompt=task_row["video_prompt"],
            target_count=task_row["target_count"],
            initial_downloaded_count=task_row["downloaded_count"],
            initial_round_count=task_row["generation_round_count"],
            source_assets=_load_source_assets(conn, task_row["task_id"]),
        )
        outcome = execute_task(
            conn=conn,
            log=log,
            flow=flow,
            workstation_id=workstation.id,
            task=task,
            config=config.generation,
            output_root=Path(config.output_root),
            run_date=run_date,
        )
        with transaction(conn):
            finalize_task(
                conn,
                cooldown_cfg=config.cooldown,
                task_id=task.task_id,
                workstation_id=workstation.id,
                final_status=outcome.final_status,
                downloaded_count=outcome.downloaded_count,
                generation_round_count=outcome.generation_round_count,
                last_error_type=outcome.last_error_type,
                last_error_message=outcome.last_error_message,
                workstation_outcome=outcome.workstation_outcome,
                result_folder=outcome.result_folder,
            )
        return outcome.final_status
    finally:
        conn.close()


def run_multi_workstation(
    *,
    db_path: Path,
    config: AppConfig,
    workstations: Iterable[WorkstationConfig],
    max_rounds: int = 0,
    use_mock: bool = False,
    mock_round_plans_per_ws: Optional[dict[str, list[MockRoundPlan]]] = None,
    mock_initial_state: PageState = PageState.READY,
    today: Optional[date] = None,
) -> MultiRunSummary:
    workstations = list(workstations)
    if not workstations:
        raise ValueError("no workstations configured")

    log = get_scheduler_logger(config.log_root)

    if not use_mock:
        for ws in workstations:
            check = check_profile(ws.browser_profile_path)
            if not check.ok:
                log.warning("profile check failed ws=%s reason=%s", ws.id, check.reason)
    sync_workstations(db_path, workstations)

    by_id = {w.id: w for w in workstations}
    summary = MultiRunSummary()
    summary_lock = threading.Lock()

    main_conn = connect(db_path)
    in_flight: dict[str, Future] = {}
    # Last workstation submit timestamp — used to stagger launches so
    # multiple workstations don't fire their Veo requests within the
    # same second from the same IP (which Google flags as bot fan-out
    # and bans every workstation simultaneously).
    last_submit_time: list[float] = [0.0]

    def _release_done() -> None:
        for ws_id in list(in_flight):
            fut = in_flight[ws_id]
            if fut.done():
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    log.error("worker thread for ws=%s raised: %s", ws_id, exc)
                in_flight.pop(ws_id, None)

    try:
        recover_workstation_states(main_conn)
        release_orphaned_busy_workstations(main_conn)
        recover_zombie_tasks(main_conn, cfg=config.recovery)

        run_date = today or date.today()
        max_workers = max(1, len(workstations))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            rounds = 0
            consecutive_idle = 0
            while True:
                if max_rounds and rounds >= max_rounds:
                    break
                rounds += 1
                _release_done()
                # Don't claim if the workstation already has an in-flight job.
                eligible_ids = [w.id for w in workstations if w.id not in in_flight]
                if not eligible_ids:
                    # All workstations busy in-thread — wait for one to free up.
                    next(iter(in_flight.values())).result()  # block on any one
                    _release_done()
                    continue

                claim = claim_one(main_conn, today=today)
                if claim is None:
                    consecutive_idle += 1
                    if not in_flight and consecutive_idle >= 1:
                        log.info("no claimable task & no in-flight worker, exiting")
                        break
                    if in_flight:
                        # Wait for one job, then try again.
                        any_future = next(iter(in_flight.values()))
                        any_future.result()
                        _release_done()
                        consecutive_idle = 0
                        continue
                    break
                consecutive_idle = 0

                ws_cfg = by_id[claim.workstation_id]
                plans = (mock_round_plans_per_ws or {}).get(ws_cfg.id)

                # Stagger workstation launches. ``inter_workstation_launch_stagger_sec``
                # is the minimum gap between consecutive submits so the
                # second-and-later workstations don't hit Google with
                # parallel Veo requests from the same IP.
                stagger_sec = getattr(
                    config.generation, "inter_workstation_launch_stagger_sec", 0
                )
                if stagger_sec > 0 and last_submit_time[0] > 0:
                    elapsed = time.time() - last_submit_time[0]
                    if elapsed < stagger_sec:
                        wait = stagger_sec - elapsed
                        log.info(
                            "[stagger] sleeping %.1fs before launching ws=%s "
                            "(last submit %.1fs ago)",
                            wait, ws_cfg.id, elapsed,
                        )
                        time.sleep(wait)
                last_submit_time[0] = time.time()

                fut = pool.submit(
                    _execute_in_thread,
                    db_path=db_path,
                    config=config,
                    workstation=ws_cfg,
                    task_row=claim.task_row,
                    use_mock=use_mock,
                    mock_round_plans=plans,
                    mock_initial_state=mock_initial_state,
                    run_date=run_date,
                )

                def _on_done(f: Future, ws_id: str = ws_cfg.id):
                    try:
                        final_status = f.result()
                    except Exception as exc:  # noqa: BLE001
                        log.error("worker thread for ws=%s raised: %s", ws_id, exc)
                        return
                    with summary_lock:
                        summary.executed += 1
                        field = {
                            "success": "success",
                            "failed": "failed",
                            "download_failed": "download_failed",
                            "retry_waiting": "retry_waiting",
                            "manual_review": "manual_review",
                        }.get(final_status, "retry_waiting")
                        setattr(summary, field, getattr(summary, field) + 1)

                fut.add_done_callback(_on_done)
                in_flight[ws_cfg.id] = fut

            # Drain any remaining work.
            for fut in list(in_flight.values()):
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    log.error("worker thread raised during drain: %s", exc)
    finally:
        main_conn.close()

    return summary

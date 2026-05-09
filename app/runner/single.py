"""Single-workstation runner (T11).

Implements the V0 flow: pick the configured workstation, claim one task at a
time, run it through the candidate generation loop, finalize state, repeat
up to ``max_tasks`` times. Stops as soon as the workstation falls out of
``healthy`` (so we don't keep retrying on a manual-check workstation).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from app.config.loader import AppConfig, FlowModeSpec, WorkstationConfig
from app.db.connection import connect, transaction
from app.scheduler.claim import claim_one
from app.scheduler.state import (
    finalize_task,
    recover_workstation_states,
    release_orphaned_busy_workstations,
)
from app.utils.logging import get_worker_logger
from app.workstations.profile_check import check_profile
from app.workstations.sync import sync_workstations
from app.worker.flow_mock import MockFlowPort, MockRoundPlan
from app.worker.flow_port import FlowPort, PageState, SourceAsset
from app.worker.loop import TaskInput, execute_task


def _load_source_assets(conn, task_id: str) -> list[SourceAsset]:
    """Read ordered task_assets rows for a task. Falls back to empty list
    if none are present — the worker will then synthesize a single
    legacy-shaped asset from ``tasks.source_asset_path``."""
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
class RunSummary:
    executed: int = 0
    success: int = 0
    failed: int = 0
    download_failed: int = 0
    retry_waiting: int = 0
    manual_review: int = 0


def _flow_port_factory(
    config: AppConfig,
    workstation: WorkstationConfig,
    *,
    use_mock: bool,
    mock_round_plans: Optional[list[MockRoundPlan]] = None,
    mock_initial_state: PageState = PageState.READY,
    task_flow_mode: Optional[FlowModeSpec] = None,
) -> FlowPort:
    if use_mock:
        plans = mock_round_plans if mock_round_plans is not None else [MockRoundPlan.success(4)]
        return MockFlowPort(round_plans=plans, initial_state=mock_initial_state)

    from app.runner.multi import _merge_flow_mode

    # V2 spike: same opt-in branch as runner.multi — see that file for
    # full notes. Single-WS path uses workstation.id as the ws_id too.
    import os as _os
    if _os.environ.get("FLOW_HARVESTER_USE_EXTENSION") == "1":
        from app.worker.flow_extension_port import ExtensionFlowPort
        return ExtensionFlowPort(
            ws_id=workstation.id,
            project_url=workstation.flow_project_url,
            page_action_timeout_sec=config.generation.page_action_timeout_sec,
            flow_mode_spec=_merge_flow_mode(workstation.flow_mode, task_flow_mode),
        )

    # Lazy import so the package is usable without patchright installed
    # for ``--mock`` runs / tests.
    try:
        from app.worker.flow_playwright import PlaywrightFlowPort  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "FlowPort not available. Install patchright or use --mock."
        ) from exc
    return PlaywrightFlowPort(
        entry_url=config.flow.entry_url,
        profile_path=Path(workstation.browser_profile_path),
        page_action_timeout_sec=config.generation.page_action_timeout_sec,
        project_url=workstation.flow_project_url,
        flow_mode_spec=_merge_flow_mode(workstation.flow_mode, task_flow_mode),
    )


def run_single_workstation(
    *,
    db_path: Path,
    config: AppConfig,
    workstations: Iterable[WorkstationConfig],
    target_workstation_id: Optional[str] = None,
    max_tasks: int = 1,
    use_mock: bool = False,
    mock_round_plans: Optional[list[MockRoundPlan]] = None,
    mock_initial_state: PageState = PageState.READY,
    today: Optional[date] = None,
) -> RunSummary:
    workstations = list(workstations)
    if target_workstation_id is None:
        if not workstations:
            raise ValueError("no workstations configured")
        target_workstation_id = workstations[0].id

    target = next((w for w in workstations if w.id == target_workstation_id), None)
    if target is None:
        raise ValueError(f"workstation not found in config: {target_workstation_id}")

    log = get_worker_logger(config.log_root, target.id)

    if not use_mock:
        check = check_profile(target.browser_profile_path)
        if not check.ok:
            log.error("profile check failed for ws=%s reason=%s", target.id, check.reason)
            raise RuntimeError(f"workstation {target.id} profile not ready: {check.reason}")

    sync_workstations(db_path, workstations)
    summary = RunSummary()

    conn = connect(db_path)
    try:
        recover_workstation_states(conn)
        release_orphaned_busy_workstations(conn)
        run_date = today or date.today()
        for _ in range(max_tasks):
            claimed = claim_one(conn, today=today, only_workstation_id=target.id)
            if claimed is None:
                log.info("no claimable task for ws=%s, stopping", target.id)
                break
            row = claimed.task_row
            task = TaskInput(
                task_id=row["task_id"],
                sku_id=row["sku_id"],
                creative_id=row["creative_id"],
                segment_id=row["segment_id"],
                source_asset_path=Path(row["source_asset_path"]),
                video_prompt=row["video_prompt"],
                target_count=row["target_count"],
                initial_downloaded_count=row["downloaded_count"],
                initial_round_count=row["generation_round_count"],
                source_assets=_load_source_assets(conn, row["task_id"]),
            )
            task_flow_mode: Optional[FlowModeSpec] = None
            fm_fields = {
                k: row.get(f"flow_mode_{k}")
                for k in ("tab", "subtab", "aspect", "output_count",
                          "duration_sec", "model")
            }
            if any(v is not None for v in fm_fields.values()):
                task_flow_mode = FlowModeSpec(**fm_fields)
            flow = _flow_port_factory(
                config,
                target,
                task_flow_mode=task_flow_mode,
                use_mock=use_mock,
                mock_round_plans=mock_round_plans,
                mock_initial_state=mock_initial_state,
            )
            outcome = execute_task(
                conn=conn,
                log=log,
                flow=flow,
                workstation_id=target.id,
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
                    workstation_id=target.id,
                    final_status=outcome.final_status,
                    downloaded_count=outcome.downloaded_count,
                    generation_round_count=outcome.generation_round_count,
                    last_error_type=outcome.last_error_type,
                    last_error_message=outcome.last_error_message,
                    workstation_outcome=outcome.workstation_outcome,
                    result_folder=outcome.result_folder,
                )
            summary.executed += 1
            counter_field = {
                "success": "success",
                "failed": "failed",
                "download_failed": "download_failed",
                "retry_waiting": "retry_waiting",
                "manual_review": "manual_review",
            }.get(outcome.final_status, "retry_waiting")
            setattr(summary, counter_field, getattr(summary, counter_field) + 1)

            ws_status = conn.execute(
                "SELECT status FROM workstations WHERE id = ?", (target.id,)
            ).fetchone()["status"]
            if ws_status != "healthy":
                log.info("ws=%s left healthy state -> %s, stopping", target.id, ws_status)
                break
    finally:
        conn.close()
    return summary

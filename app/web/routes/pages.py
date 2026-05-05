"""HTML page routes (server-rendered Jinja2).

These pages are what the customer sees in the browser. Form submits
re-use the JSON API code paths via repository imports rather than
re-implementing CRUD here, so adding a field once (in the repository)
shows up everywhere.

A note on form vs JSON: the customer-facing forms post to ``/workstations``
/ ``/tasks/new`` (this module) and get back HTML redirects. HTMX widgets
on the same pages talk straight to ``/api/...`` for inline operations
(delete, scheduler stop/start, etc.) — those return JSON.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import paths as app_paths
from app.config.loader import FlowModeSpec, WorkstationConfig
from app.scheduler.daemon import SchedulerDaemon
from app.tasks.repository import (
    AssetDraft,
    TaskDraft,
    TaskRepositoryError,
    create_task,
    get_task,
    get_task_assets,
    list_tasks,
)
from app.web.dependencies import get_config, get_daemon, get_db_conn
from app.workstations.repository import (
    WorkstationConflictError,
    WorkstationNotFoundError,
    create_workstation,
    get_workstation,
    get_workstation_health,
    get_workstation_health_history,
    list_workstations,
    update_workstation_config,
)


router = APIRouter(tags=["pages"], include_in_schema=False)


_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _scheduler_dict(daemon: SchedulerDaemon) -> dict:
    """Snapshot the daemon status into a dict the templates can index into.
    Templates avoid attribute access so adding fields stays backward compat."""
    snap = daemon.status()
    return {
        "running": snap.running,
        "started_at": snap.started_at,
        "stopped_at": snap.stopped_at,
        "last_round_at": snap.last_round_at,
        "rounds_completed": snap.rounds_completed,
        "last_error": snap.last_error,
        "cumulative": {
            "executed": snap.cumulative.executed,
            "success": snap.cumulative.success,
            "failed": snap.cumulative.failed,
            "download_failed": snap.cumulative.download_failed,
            "retry_waiting": snap.cumulative.retry_waiting,
            "manual_review": snap.cumulative.manual_review,
        },
    }


def _has_file(upload: Optional[UploadFile]) -> bool:
    """Whether a multipart UploadFile slot actually contains a file.

    A user that submits a form without picking a file in an optional
    file input still produces a non-None ``UploadFile`` with empty
    ``filename`` (and an empty in-memory body). Treat that as absent
    so callers can skip past it cleanly.
    """
    if upload is None:
        return False
    name = (upload.filename or "").strip()
    return bool(name)


def _empty_or_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def _empty_or_str(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    return value


def _build_flow_mode(
    *,
    tab: Optional[str],
    subtab: Optional[str],
    aspect: Optional[str],
    output_count: Optional[int],
    duration_sec: Optional[int],
    model: Optional[str],
) -> Optional[FlowModeSpec]:
    fields = {
        "tab": _empty_or_str(tab),
        "subtab": _empty_or_str(subtab),
        "aspect": _empty_or_str(aspect),
        "output_count": output_count,
        "duration_sec": duration_sec,
        "model": _empty_or_str(model),
    }
    if all(v is None for v in fields.values()):
        return None
    return FlowModeSpec(**fields)


# ---------------------------------------------------------------- dashboard


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db_conn),
    daemon: SchedulerDaemon = Depends(get_daemon),
) -> HTMLResponse:
    ws_with_health = [
        {"ws": ws, "health": get_workstation_health(conn, ws.id)}
        for ws in list_workstations(conn)
    ]
    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "active_page": "dashboard",
            "workstations": ws_with_health,
            "tasks": list_tasks(conn, limit=10),
            "daemon": _scheduler_dict(daemon),
        },
    )


# --------------------------------------------------------------- workstations


@router.get("/workstations", response_class=HTMLResponse)
def workstations_page(
    request: Request, conn: sqlite3.Connection = Depends(get_db_conn)
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "workstations.html",
        {
            "active_page": "workstations",
            "workstations": list_workstations(conn),
        },
    )


@router.get("/workstations/new", response_class=HTMLResponse)
def workstation_new_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "workstation_form.html",
        {
            "active_page": "workstations",
            "ws": None,
            "form_action": "/workstations/new",
        },
    )


@router.post("/workstations/new")
def workstation_create(
    id: str = Form(...),
    account_label: str = Form(...),
    daily_task_limit: int = Form(...),
    browser_profile_path: Optional[str] = Form(None),
    flow_project_url: Optional[str] = Form(None),
    mode_tab: Optional[str] = Form(None),
    mode_subtab: Optional[str] = Form(None),
    mode_aspect: Optional[str] = Form(None),
    mode_output_count: Optional[str] = Form(None),
    mode_duration_sec: Optional[str] = Form(None),
    mode_model: Optional[str] = Form(None),
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> RedirectResponse:
    profile_path = browser_profile_path or str(app_paths.workstation_profile_path(id))
    try:
        ws = WorkstationConfig(
            id=id,
            account_label=account_label,
            browser_profile_path=profile_path,
            daily_task_limit=daily_task_limit,
            flow_project_url=_empty_or_str(flow_project_url),
            flow_mode=_build_flow_mode(
                tab=mode_tab, subtab=mode_subtab, aspect=mode_aspect,
                output_count=_empty_or_int(mode_output_count),
                duration_sec=_empty_or_int(mode_duration_sec),
                model=mode_model,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError, etc.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        create_workstation(conn, ws)
    except WorkstationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    Path(profile_path).mkdir(parents=True, exist_ok=True)
    return RedirectResponse(url=f"/workstations/{id}", status_code=303)


@router.get("/workstations/{ws_id}", response_class=HTMLResponse)
def workstation_detail(
    request: Request,
    ws_id: str,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> HTMLResponse:
    ws = get_workstation(conn, ws_id)
    if ws is None:
        raise HTTPException(status_code=404, detail=f"workstation not found: {ws_id}")
    rows = conn.execute(
        "SELECT task_id, status, downloaded_count, target_count "
        "FROM tasks WHERE assigned_workstation_id = ? "
        "ORDER BY created_at DESC LIMIT 20",
        (ws_id,),
    ).fetchall()
    recent_tasks = [
        {
            "task_id": r["task_id"], "status": r["status"],
            "downloaded_count": r["downloaded_count"],
            "target_count": r["target_count"],
        }
        for r in rows
    ]
    health = get_workstation_health(conn, ws_id)
    history = get_workstation_health_history(conn, ws_id)
    return templates.TemplateResponse(
        request, "workstation_detail.html",
        {
            "active_page": "workstations",
            "ws": ws,
            "health": health,
            "history": history,
            "recent_tasks": recent_tasks,
        },
    )


@router.get("/workstations/{ws_id}/edit", response_class=HTMLResponse)
def workstation_edit_form(
    request: Request, ws_id: str,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> HTMLResponse:
    ws = get_workstation(conn, ws_id)
    if ws is None:
        raise HTTPException(status_code=404, detail=f"workstation not found: {ws_id}")
    return templates.TemplateResponse(
        request, "workstation_form.html",
        {
            "active_page": "workstations",
            "ws": ws,
            "form_action": f"/workstations/{ws_id}/edit",
        },
    )


@router.post("/workstations/{ws_id}/edit")
def workstation_update(
    ws_id: str,
    account_label: Optional[str] = Form(None),
    daily_task_limit: Optional[int] = Form(None),
    browser_profile_path: Optional[str] = Form(None),
    flow_project_url: Optional[str] = Form(None),
    mode_tab: Optional[str] = Form(None),
    mode_subtab: Optional[str] = Form(None),
    mode_aspect: Optional[str] = Form(None),
    mode_output_count: Optional[str] = Form(None),
    mode_duration_sec: Optional[str] = Form(None),
    mode_model: Optional[str] = Form(None),
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> RedirectResponse:
    fields: dict = {}
    if account_label:
        fields["account_label"] = account_label
    if daily_task_limit is not None:
        fields["daily_task_limit"] = daily_task_limit
    if browser_profile_path:
        fields["browser_profile_path"] = browser_profile_path
    # flow_project_url: empty string clears the field, missing means "no change".
    if flow_project_url is not None:
        fields["flow_project_url"] = _empty_or_str(flow_project_url)
    # flow_mode: only touch the preset when the operator explicitly
    # submitted at least one mode_* field. The simplified edit form
    # doesn't carry these so they stay None and the existing preset
    # (set by the login flow) survives.
    if any(v is not None and v != "" for v in (
        mode_tab, mode_subtab, mode_aspect,
        mode_output_count, mode_duration_sec, mode_model,
    )):
        fields["flow_mode"] = _build_flow_mode(
            tab=mode_tab, subtab=mode_subtab, aspect=mode_aspect,
            output_count=_empty_or_int(mode_output_count),
            duration_sec=_empty_or_int(mode_duration_sec),
            model=mode_model,
        )
    try:
        update_workstation_config(conn, ws_id, **fields)
    except WorkstationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=f"/workstations/{ws_id}", status_code=303)


# --------------------------------------------------------------------- tasks


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(
    request: Request,
    status: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "tasks.html",
        {
            "active_page": "tasks",
            "tasks": list_tasks(conn, status=status, limit=200),
            "current_status": status,
        },
    )


@router.get("/tasks/new", response_class=HTMLResponse)
def tasks_new_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "task_form.html",
        {"active_page": "new_task"},
    )


@router.get("/tasks/bulk", response_class=HTMLResponse)
def tasks_bulk_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "task_bulk.html",
        {"active_page": "tasks"},
    )


@router.post("/tasks/new")
async def tasks_create(
    sku_id: str = Form(...),
    creative_id: str = Form(...),
    segment_id: str = Form(...),
    video_prompt: str = Form(...),
    target_count: int = Form(...),
    asset_kind: str = Form("reference"),
    assets: list[UploadFile] = File(default_factory=list),
    asset_start: Optional[UploadFile] = File(None),
    asset_end: Optional[UploadFile] = File(None),
    mode_tab: Optional[str] = Form(None),
    mode_subtab: Optional[str] = Form(None),
    mode_aspect: Optional[str] = Form(None),
    mode_output_count: Optional[str] = Form(None),
    mode_duration_sec: Optional[str] = Form(None),
    mode_model: Optional[str] = Form(None),
    conn: sqlite3.Connection = Depends(get_db_conn),
    config=Depends(get_config),
) -> RedirectResponse:
    # ``frames_pair`` and ``first_frame`` are Frames-mode shortcuts:
    # they read the dedicated asset_start (and asset_end, for pair) so
    # start vs end is unambiguous regardless of OS file dialog
    # selection order. Both auto-promote ``mode_subtab=frames`` so the
    # worker switches the UI tab even if the operator forgot to flip
    # the subtab dropdown. All other kinds use the single multi-file
    # ``assets`` input.
    if asset_kind == "frames_pair":
        if asset_start is None or not _has_file(asset_start):
            raise HTTPException(
                status_code=400,
                detail="Frames 模式必须上传起始帧（Start）",
            )
        if asset_end is None or not _has_file(asset_end):
            raise HTTPException(
                status_code=400,
                detail="Frames 模式必须上传结束帧（End）",
            )
        ordered_uploads = [
            (asset_start, "first_frame"),
            (asset_end, "last_frame"),
        ]
        if mode_subtab in (None, "", "ingredients"):
            mode_subtab = "frames"
    elif asset_kind == "first_frame":
        if asset_start is None or not _has_file(asset_start):
            raise HTTPException(
                status_code=400,
                detail="仅首帧模式必须上传起始帧（Start）",
            )
        ordered_uploads = [(asset_start, "first_frame")]
        if mode_subtab in (None, "", "ingredients"):
            mode_subtab = "frames"
    else:
        if not assets or all(not _has_file(a) for a in assets):
            raise HTTPException(
                status_code=400, detail="at least one asset required",
            )
        ordered_uploads = [(a, asset_kind) for a in assets if _has_file(a)]

    tmp_dir = Path(tempfile.mkdtemp(prefix="flow_upload_"))
    try:
        asset_drafts: list[AssetDraft] = []
        for idx, (upload, kind) in enumerate(ordered_uploads, start=1):
            safe_name = (upload.filename or f"upload_{idx}.bin").replace("/", "_")
            dest = tmp_dir / f"{idx:02d}_{safe_name}"
            with dest.open("wb") as fh:
                while True:
                    chunk = await upload.read(64 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            asset_drafts.append(AssetDraft(
                path=dest, kind=kind, copy_into_managed_dir=True,
            ))
        flow_mode = _build_flow_mode(
            tab=mode_tab, subtab=mode_subtab, aspect=mode_aspect,
            output_count=_empty_or_int(mode_output_count),
            duration_sec=_empty_or_int(mode_duration_sec),
            model=mode_model,
        )
        draft = TaskDraft(
            sku_id=sku_id, creative_id=creative_id, segment_id=segment_id,
            video_prompt=video_prompt, target_count=target_count,
            assets=asset_drafts,
            flow_mode=flow_mode,
        )
        try:
            new_id = create_task(
                conn, draft,
                default_max_retry=config.generation.max_retry_count,
            )
        except TaskRepositoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return RedirectResponse(url=f"/tasks/{new_id}", status_code=303)


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(
    request: Request, task_id: str,
    conn: sqlite3.Connection = Depends(get_db_conn),
    config=Depends(get_config),
) -> HTMLResponse:
    record = get_task(conn, task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    from app.web.routes.ws import _task_results_for_render
    results = _task_results_for_render(
        conn, task_id, Path(config.output_root),
    )
    return templates.TemplateResponse(
        request, "task_detail.html",
        {
            "active_page": "tasks",
            "task": record,
            "assets": get_task_assets(conn, task_id),
            "results": results,
        },
    )

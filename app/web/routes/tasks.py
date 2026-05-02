"""Task JSON API.

Backed by ``app.tasks.repository``. ``POST /api/tasks`` accepts
``multipart/form-data`` with one or more uploaded image files; the server
streams them to a temp dir, hands the paths to ``create_task`` (which
copies them into the managed assets dir), and discards the temp copies.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.tasks.repository import (
    AssetDraft,
    TaskDraft,
    TaskRepositoryError,
    create_task,
    delete_task,
    get_task,
    get_task_assets,
    list_tasks,
)
from app.web.dependencies import get_config, get_db_conn


router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskAssetOut(BaseModel):
    order: int
    path: str
    kind: str


class TaskOut(BaseModel):
    task_id: str
    sku_id: str
    creative_id: str
    segment_id: str
    sequence_index: int
    video_prompt: str
    target_count: int
    downloaded_count: int
    generation_round_count: int
    status: str
    assigned_workstation_id: Optional[str]
    retry_count: int
    max_retry_count: int
    depends_on_task_id: Optional[str]
    error_type: Optional[str]
    error_message: Optional[str]
    created_at: str
    assets: list[TaskAssetOut] = []


def _to_out(record, assets: list[tuple[int, str, str]] | None = None) -> TaskOut:
    return TaskOut(
        task_id=record.task_id,
        sku_id=record.sku_id,
        creative_id=record.creative_id,
        segment_id=record.segment_id,
        sequence_index=record.sequence_index,
        video_prompt=record.video_prompt,
        target_count=record.target_count,
        downloaded_count=record.downloaded_count,
        generation_round_count=record.generation_round_count,
        status=record.status,
        assigned_workstation_id=record.assigned_workstation_id,
        retry_count=record.retry_count,
        max_retry_count=record.max_retry_count,
        depends_on_task_id=record.depends_on_task_id,
        error_type=record.error_type,
        error_message=record.error_message,
        created_at=record.created_at,
        assets=[
            TaskAssetOut(order=o, path=p, kind=k)
            for o, p, k in (assets or [])
        ],
    )


@router.get("", response_model=list[TaskOut])
def list_route(
    status: Optional[str] = None,
    limit: int = 50,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> list[TaskOut]:
    return [_to_out(r) for r in list_tasks(conn, status=status, limit=limit)]


@router.get("/{task_id}", response_model=TaskOut)
def get_route(task_id: str, conn: sqlite3.Connection = Depends(get_db_conn)) -> TaskOut:
    record = get_task(conn, task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    assets = get_task_assets(conn, task_id)
    return _to_out(record, assets)


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_route(
    sku_id: str = Form(...),
    creative_id: str = Form(...),
    segment_id: str = Form(...),
    video_prompt: str = Form(...),
    target_count: int = Form(...),
    asset_kind: str = Form("reference"),
    sequence_index: int = Form(1),
    depends_on_task_id: Optional[str] = Form(None),
    max_retry_count: Optional[int] = Form(None),
    assets: List[UploadFile] = File(..., description="One or more image files"),
    conn: sqlite3.Connection = Depends(get_db_conn),
    config=Depends(get_config),
) -> TaskOut:
    """Create a task from a multipart form, copying uploads into managed dir."""
    if not assets:
        raise HTTPException(status_code=400, detail="at least one asset file required")

    # Stream uploads to a temp dir; create_task() copies into assets_dir
    # and we discard the temps in finally.
    tmp_dir = Path(tempfile.mkdtemp(prefix="flow_upload_"))
    try:
        asset_drafts: list[AssetDraft] = []
        for idx, upload in enumerate(assets, start=1):
            safe_name = (upload.filename or f"upload_{idx}.bin").replace("/", "_")
            dest = tmp_dir / f"{idx:02d}_{safe_name}"
            with dest.open("wb") as fh:
                while True:
                    chunk = await upload.read(64 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            asset_drafts.append(AssetDraft(
                path=dest, kind=asset_kind, copy_into_managed_dir=True,
            ))

        draft = TaskDraft(
            sku_id=sku_id,
            creative_id=creative_id,
            segment_id=segment_id,
            video_prompt=video_prompt,
            target_count=target_count,
            assets=asset_drafts,
            sequence_index=sequence_index,
            depends_on_task_id=depends_on_task_id,
            max_retry_count=max_retry_count,
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

    record = get_task(conn, new_id)
    assert record is not None
    return _to_out(record, get_task_assets(conn, new_id))


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_route(
    task_id: str,
    force: bool = False,
    keep_assets: bool = False,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> None:
    try:
        ok = delete_task(
            conn, task_id, force=force, remove_assets=not keep_assets,
        )
    except TaskRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")

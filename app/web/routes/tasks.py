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
    resume_task,
)
from app.web.dependencies import get_config, get_db_conn


router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskAssetOut(BaseModel):
    order: int
    path: str
    kind: str


class TaskFlowModeOut(BaseModel):
    tab: Optional[str] = None
    subtab: Optional[str] = None
    aspect: Optional[str] = None
    output_count: Optional[int] = None
    duration_sec: Optional[int] = None
    model: Optional[str] = None


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
    flow_mode: Optional[TaskFlowModeOut] = None
    assets: list[TaskAssetOut] = []


def _to_out(record, assets: list[tuple[int, str, str]] | None = None) -> TaskOut:
    fm_out: Optional[TaskFlowModeOut] = None
    if record.flow_mode is not None:
        fm_out = TaskFlowModeOut(**record.flow_mode.model_dump())
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
        flow_mode=fm_out,
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


class BulkImportSummary(BaseModel):
    inserted: int
    skipped: int
    errors: list[str]
    task_ids: list[str]


@router.post("/bulk-import", response_model=BulkImportSummary,
             status_code=status.HTTP_201_CREATED)
async def bulk_import_route(
    csv_file: UploadFile = File(..., description="CSV with task rows"),
    images: List[UploadFile] = File(..., description="All referenced images"),
    conn: sqlite3.Connection = Depends(get_db_conn),
    config=Depends(get_config),
) -> BulkImportSummary:
    """Import many tasks at once from a CSV + a folder of images.

    CSV columns: ``sku_id, creative_id, segment_id, video_prompt,
    target_count, source_asset_path``. ``source_asset_path`` references
    one of the uploaded image files by basename — the server resolves it
    after staging the uploads under a temp dir.

    Optional columns: ``sequence_index``, ``depends_on_task_id``,
    ``max_retry_count``, ``asset_kind``. Anything not in the CSV uses the
    same defaults as the single-task form.
    """
    import csv as _csv
    import io as _io
    import shutil as _shutil
    import tempfile as _tempfile
    from app.config.loader import FlowModeSpec
    from app.tasks.repository import (
        AssetDraft, TaskDraft, TaskRepositoryError,
    )

    def _empty_or_int(value):
        if value is None or str(value).strip() == "":
            return None
        return int(value)

    def _empty_or_str(value):
        if value is None or str(value).strip() == "":
            return None
        return str(value).strip()

    def _row_flow_mode(row: dict) -> FlowModeSpec | None:
        fields = {
            "tab": _empty_or_str(row.get("mode_tab")),
            "subtab": _empty_or_str(row.get("mode_subtab")),
            "aspect": _empty_or_str(row.get("mode_aspect")),
            "output_count": _empty_or_int(row.get("mode_output_count")),
            "duration_sec": _empty_or_int(row.get("mode_duration_sec")),
            "model": _empty_or_str(row.get("mode_model")),
        }
        if all(v is None for v in fields.values()):
            return None
        return FlowModeSpec(**fields)

    try:
        text = (await csv_file.read()).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"CSV must be UTF-8: {exc}",
        ) from exc
    reader = _csv.DictReader(_io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="CSV has no rows")

    # Stage every uploaded image under a temp dir keyed by basename.
    tmp_dir = Path(_tempfile.mkdtemp(prefix="flow_bulk_"))
    by_basename: dict[str, Path] = {}
    try:
        for upload in images:
            base = (upload.filename or "").split("/")[-1]
            if not base:
                continue
            dest = tmp_dir / base
            with dest.open("wb") as fh:
                while True:
                    chunk = await upload.read(64 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            by_basename[base] = dest

        inserted: list[str] = []
        errors: list[str] = []
        for line_no, row in enumerate(rows, start=2):
            try:
                asset_ref = (row.get("source_asset_path") or "").strip()
                if not asset_ref:
                    raise ValueError("source_asset_path is empty")
                # Pipe-separated multi-asset support: same shape as the
                # legacy CLI importer so existing customer CSVs work.
                # Path prefixes (``input/images/p1i1.jpg``) are stripped
                # to the basename so the user can paste in whatever path
                # form their spreadsheet has.
                asset_refs = [
                    p.strip().split("/")[-1].split("\\")[-1]
                    for p in asset_ref.split("|") if p.strip()
                ]
                if not asset_refs:
                    raise ValueError("source_asset_path has no entries")
                staged_paths: list[Path] = []
                for ref in asset_refs:
                    staged = by_basename.get(ref)
                    if staged is None:
                        raise ValueError(
                            f"image {ref!r} not in uploaded files"
                        )
                    staged_paths.append(staged)
                # ``asset_kind`` is the new column; accept the legacy
                # ``source_asset_type`` as an alias so old CSVs work.
                kind = (
                    (row.get("asset_kind") or row.get("source_asset_type")
                     or "reference").strip()
                )
                target = int((row.get("target_count") or "").strip() or "0")
                draft = TaskDraft(
                    sku_id=(row.get("sku_id") or "").strip(),
                    creative_id=(row.get("creative_id") or "").strip(),
                    segment_id=(row.get("segment_id") or "").strip(),
                    video_prompt=(row.get("video_prompt") or "").strip(),
                    target_count=target,
                    sequence_index=int(
                        (row.get("sequence_index") or "1").strip() or "1"
                    ),
                    depends_on_task_id=(
                        (row.get("depends_on_task_id") or "").strip() or None
                    ),
                    max_retry_count=(
                        int(row["max_retry_count"])
                        if row.get("max_retry_count")
                        and str(row["max_retry_count"]).strip() != ""
                        else None
                    ),
                    assets=[
                        AssetDraft(path=p, kind=kind, copy_into_managed_dir=True)
                        for p in staged_paths
                    ],
                    flow_mode=_row_flow_mode(row),
                )
                new_id = create_task(
                    conn, draft,
                    default_max_retry=config.generation.max_retry_count,
                )
                inserted.append(new_id)
            except (TaskRepositoryError, ValueError, KeyError) as exc:
                errors.append(f"第 {line_no} 行：{exc}")
    finally:
        _shutil.rmtree(tmp_dir, ignore_errors=True)

    return BulkImportSummary(
        inserted=len(inserted),
        skipped=len(errors),
        errors=errors,
        task_ids=inserted,
    )


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


@router.post("/{task_id}/resume", response_model=TaskOut)
def resume_route(
    task_id: str,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> TaskOut:
    """Continue a task whose retry budget was exhausted.

    Clears ``retry_count`` and flips status back to ``pending`` while
    leaving ``downloaded_count`` and ``task_results`` intact. The next
    scheduler pass will claim it and resume from the existing progress.
    """
    if not resume_task(conn, task_id):
        record = get_task(conn, task_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        raise HTTPException(
            status_code=409,
            detail=f"task {task_id} is in status '{record.status}' — cannot resume",
        )
    refreshed = get_task(conn, task_id)
    assert refreshed is not None
    return _to_out(refreshed, get_task_assets(conn, task_id))


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

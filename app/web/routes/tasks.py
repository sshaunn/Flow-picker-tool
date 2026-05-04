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


# ----------------------------------------------------------------------------
# Bulk-import row parsing helpers (shared by /bulk-import and /bulk-validate).
# Lives at module level so the dry-run validate route can reuse the exact
# same validation rules; otherwise the two routes drift and a CSV that
# preview-passes might still fail at import.

def _bulk_empty_or_int(value):
    if value is None or str(value).strip() == "":
        return None
    return int(value)


def _bulk_empty_or_str(value):
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def _bulk_row_flow_mode(row: dict, *, force_subtab_frames: bool = False):
    from app.config.loader import FlowModeSpec
    fields = {
        "tab": _bulk_empty_or_str(row.get("mode_tab")),
        "subtab": _bulk_empty_or_str(row.get("mode_subtab")),
        "aspect": _bulk_empty_or_str(row.get("mode_aspect")),
        "output_count": _bulk_empty_or_int(row.get("mode_output_count")),
        "duration_sec": _bulk_empty_or_int(row.get("mode_duration_sec")),
        "model": _bulk_empty_or_str(row.get("mode_model")),
    }
    if force_subtab_frames and fields["subtab"] in (None, "ingredients"):
        fields["subtab"] = "frames"
    if all(v is None for v in fields.values()):
        return None
    return FlowModeSpec(**fields)


def _bulk_split_pipe(raw: str) -> list[str]:
    return [
        p.strip().split("/")[-1].split("\\")[-1]
        for p in (raw or "").split("|") if p.strip()
    ]


def _bulk_resolve_row_assets(
    row: dict, by_basename: dict[str, Path],
) -> tuple[list[tuple[Path, str]], bool]:
    """Resolve a CSV row's asset references → ``(path, kind)`` pairs.

    Returns ``(asset_pairs, is_frames_row)`` so the caller can force
    ``mode_subtab=frames`` on frames-mode rows. Cross-column conflicts
    (e.g. asset_kind=first_frame but image written to source_asset_path)
    raise ValueError with a Chinese message naming the wrong column —
    surfaced to the operator both via the live import errors list and
    via the dry-run preview.
    """
    kind = (
        (row.get("asset_kind") or row.get("source_asset_type")
         or "reference").strip()
    )
    ref = (row.get("source_asset_path") or "").strip()
    start = (row.get("source_start_path") or "").strip()
    end = (row.get("source_end_path") or "").strip()

    def _stage(name: str) -> Path:
        cleaned = name.strip().split("/")[-1].split("\\")[-1]
        staged = by_basename.get(cleaned)
        if staged is None:
            raise ValueError(f"图片 {cleaned!r} 不在上传文件中")
        return staged

    if kind == "frames_pair":
        if ref:
            raise ValueError(
                "asset_kind=frames_pair 不能用 source_asset_path 列；"
                "改用 source_start_path 和 source_end_path 两列"
            )
        if not start:
            raise ValueError("frames_pair 必须填 source_start_path")
        if not end:
            raise ValueError("frames_pair 必须填 source_end_path")
        return ([(_stage(start), "first_frame"),
                 (_stage(end), "last_frame")], True)

    if kind == "first_frame":
        if ref:
            raise ValueError(
                "asset_kind=first_frame 不能用 source_asset_path 列；"
                "改用 source_start_path 列"
            )
        if end:
            raise ValueError(
                "asset_kind=first_frame 时 source_end_path 必须为空"
            )
        if not start:
            raise ValueError("first_frame 必须填 source_start_path")
        return ([(_stage(start), "first_frame")], True)

    if kind == "last_frame":
        if ref:
            raise ValueError(
                "asset_kind=last_frame 不能用 source_asset_path 列；"
                "改用 source_end_path 列"
            )
        if start:
            raise ValueError(
                "asset_kind=last_frame 时 source_start_path 必须为空"
            )
        if not end:
            raise ValueError("last_frame 必须填 source_end_path")
        return ([(_stage(end), "last_frame")], True)

    # Non-frames kinds (reference / previous_segment_frame). Reject
    # any frames columns to catch a likely typo (e.g. operator put a
    # start frame in source_asset_path but forgot to switch asset_kind
    # to first_frame).
    if start or end:
        raise ValueError(
            f"asset_kind={kind} 时不能填 source_start_path "
            "或 source_end_path（这两列只用于 frames 模式）"
        )
    if not ref:
        raise ValueError("source_asset_path 为空")
    names = _bulk_split_pipe(ref)
    if not names:
        raise ValueError("source_asset_path 没有有效条目")
    return ([(_stage(n), kind) for n in names], False)


async def _bulk_decode_csv(csv_file: UploadFile) -> list[dict]:
    """Decode + parse the uploaded CSV into a list of row dicts.
    Raises HTTPException(400) for unreadable / empty CSVs."""
    import csv as _csv
    import io as _io

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
    return rows


async def _bulk_stage_images(
    images: list[UploadFile], tmp_dir: Path,
) -> dict[str, Path]:
    """Persist each uploaded image to the temp dir, keyed by basename
    so CSV ``source_asset_path`` lookups can resolve."""
    by_basename: dict[str, Path] = {}
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
    return by_basename


def _bulk_summarize_assets(
    pairs: list[tuple[Path, str]], kind_label: str,
) -> str:
    """Render a one-line preview of an asset list, e.g.
    ``frames_pair: start.png + end.png``."""
    names = " + ".join(p.name for p, _k in pairs)
    return f"{kind_label}: {names}" if names else kind_label


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


class BulkPreviewRow(BaseModel):
    """One row of a bulk-import dry-run, surfaced row-by-row in the UI
    so the operator can fix the CSV before any tasks land in the DB."""
    line_no: int
    ok: bool
    sku_id: str = ""
    creative_id: str = ""
    segment_id: str = ""
    target_count: Optional[int] = None
    asset_summary: str = ""        # human-readable e.g. "frames_pair: start.png + end.png"
    flow_mode_summary: str = ""    # e.g. "subtab=frames, aspect=9:16"
    error: Optional[str] = None    # populated only when ok=False


class BulkPreviewSummary(BaseModel):
    valid: int
    invalid: int
    rows: list[BulkPreviewRow]


@router.post("/bulk-import", response_model=BulkImportSummary,
             status_code=status.HTTP_201_CREATED)
async def bulk_import_route(
    csv_file: UploadFile = File(..., description="CSV with task rows"),
    images: List[UploadFile] = File(..., description="All referenced images"),
    conn: sqlite3.Connection = Depends(get_db_conn),
    config=Depends(get_config),
) -> BulkImportSummary:
    """Import many tasks at once from a CSV + a folder of images.

    Required CSV columns: ``sku_id, creative_id, segment_id,
    video_prompt, target_count``.

    Asset columns — pick the one matching ``asset_kind``:

    * ``asset_kind=reference`` (default): use ``source_asset_path``,
      pipe-separated for multiple images. ``source_start_path`` /
      ``source_end_path`` must be empty.
    * ``asset_kind=frames_pair``: use ``source_start_path`` AND
      ``source_end_path`` (one image each). ``source_asset_path`` must
      be empty. Auto-sets ``mode_subtab=frames``.
    * ``asset_kind=first_frame``: use ``source_start_path`` only.
      ``source_end_path`` and ``source_asset_path`` must be empty.
      Auto-sets ``mode_subtab=frames``. (Veo synthesizes the ending.)
    * ``asset_kind=last_frame`` / ``previous_segment_frame``: legacy
      kinds still accepted via ``source_asset_path``.

    Cross-column conflicts (e.g. asset_kind=first_frame but the image
    written into source_asset_path) are rejected with a clear error so
    a typo doesn't silently produce a misrouted task.

    Other optional columns: ``sequence_index``, ``depends_on_task_id``,
    ``max_retry_count``, ``mode_*``. Anything not in the CSV uses the
    same defaults as the single-task form.
    """
    import shutil as _shutil
    import tempfile as _tempfile
    from app.tasks.repository import (
        AssetDraft, TaskDraft, TaskRepositoryError,
    )

    rows = await _bulk_decode_csv(csv_file)

    # Stage every uploaded image under a temp dir keyed by basename.
    tmp_dir = Path(_tempfile.mkdtemp(prefix="flow_bulk_"))
    try:
        by_basename = await _bulk_stage_images(images, tmp_dir)

        inserted: list[str] = []
        errors: list[str] = []
        for line_no, row in enumerate(rows, start=2):
            try:
                resolved, is_frames_row = _bulk_resolve_row_assets(
                    row, by_basename,
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
                        AssetDraft(path=p, kind=k, copy_into_managed_dir=True)
                        for (p, k) in resolved
                    ],
                    flow_mode=_bulk_row_flow_mode(
                        row, force_subtab_frames=is_frames_row,
                    ),
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


@router.post("/bulk-validate", response_model=BulkPreviewSummary)
async def bulk_validate_route(
    csv_file: UploadFile = File(..., description="CSV with task rows"),
    images: List[UploadFile] = File(
        default_factory=list,
        description="Image files referenced by the CSV (optional for "
                    "validation — missing images are surfaced as errors "
                    "without aborting the rest of the preview)",
    ),
) -> BulkPreviewSummary:
    """Dry-run preview of a bulk import.

    Validates every row exactly as ``/bulk-import`` would and returns
    a per-row pass/fail breakdown so the operator can fix the CSV
    before any task lands in the DB. No tasks are created. The temp
    image dir is cleaned up before the response is sent.

    Compared to the live import (which inserts as it goes, then lists
    skipped rows), this surfaces every problem up-front so the
    operator doesn't have to clean up half-imported state.
    """
    import shutil as _shutil
    import tempfile as _tempfile

    rows = await _bulk_decode_csv(csv_file)

    tmp_dir = Path(_tempfile.mkdtemp(prefix="flow_bulk_validate_"))
    try:
        by_basename = await _bulk_stage_images(images, tmp_dir)

        preview_rows: list[BulkPreviewRow] = []
        for line_no, row in enumerate(rows, start=2):
            sku = (row.get("sku_id") or "").strip()
            cre = (row.get("creative_id") or "").strip()
            seg = (row.get("segment_id") or "").strip()
            target_str = (row.get("target_count") or "").strip()
            try:
                target = int(target_str) if target_str else 0
            except ValueError:
                target = None

            try:
                resolved, is_frames_row = _bulk_resolve_row_assets(
                    row, by_basename,
                )
                kind_label = (
                    (row.get("asset_kind") or row.get("source_asset_type")
                     or "reference").strip()
                )
                # Validate scalar required fields explicitly so the
                # preview catches them too — mirrors what
                # ``create_task`` would reject.
                if not sku:
                    raise ValueError("sku_id 不能为空")
                if not cre:
                    raise ValueError("creative_id 不能为空")
                if not seg:
                    raise ValueError("segment_id 不能为空")
                if not (row.get("video_prompt") or "").strip():
                    raise ValueError("video_prompt 不能为空")
                if target is None or target <= 0:
                    raise ValueError(
                        f"target_count 必须是正整数（当前 {target_str!r}）"
                    )

                # Build a flow_mode dict purely so we can summarize it
                # for the preview without persisting anything.
                fm = _bulk_row_flow_mode(
                    row, force_subtab_frames=is_frames_row,
                )
                fm_parts: list[str] = []
                if fm is not None:
                    for k in ("subtab", "aspect", "model",
                              "output_count", "duration_sec"):
                        v = getattr(fm, k, None)
                        if v is not None:
                            fm_parts.append(f"{k}={v}")

                preview_rows.append(BulkPreviewRow(
                    line_no=line_no, ok=True,
                    sku_id=sku, creative_id=cre, segment_id=seg,
                    target_count=target,
                    asset_summary=_bulk_summarize_assets(
                        resolved, kind_label,
                    ),
                    flow_mode_summary=", ".join(fm_parts),
                ))
            except (ValueError, KeyError) as exc:
                preview_rows.append(BulkPreviewRow(
                    line_no=line_no, ok=False,
                    sku_id=sku, creative_id=cre, segment_id=seg,
                    target_count=target if target is not None else 0,
                    error=str(exc),
                ))
    finally:
        _shutil.rmtree(tmp_dir, ignore_errors=True)

    return BulkPreviewSummary(
        valid=sum(1 for r in preview_rows if r.ok),
        invalid=sum(1 for r in preview_rows if not r.ok),
        rows=preview_rows,
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

    # ``frames_pair`` is a synthetic UI shortcut for Frames mode — the
    # form sends one kind value but we need to split: image #1 becomes
    # ``first_frame`` (Start slot), image #2 becomes ``last_frame``
    # (End slot). Reject any other count to avoid a half-configured
    # frames task that the worker can't route.
    if asset_kind == "frames_pair":
        if len(assets) != 2:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Frames 模式必须上传 2 张图片（第 1 张作为 Start，"
                    "第 2 张作为 End），收到 "
                    f"{len(assets)} 张"
                ),
            )
        per_asset_kinds = ["first_frame", "last_frame"]
    else:
        per_asset_kinds = [asset_kind] * len(assets)

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
                path=dest, kind=per_asset_kinds[idx - 1],
                copy_into_managed_dir=True,
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

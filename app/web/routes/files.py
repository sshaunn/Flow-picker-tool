"""Generated-video file serving + open-folder helper + worker log tail.

These routes power the task detail page's video grid, "open folder"
button, and live worker log section. Everything is bound to localhost
in V1 so we don't need auth — but path traversal still has to be
guarded because a malicious browser tab could craft URLs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from app.config.loader import AppConfig
from app.db.connection import connect
from app.tasks.repository import get_task
from app.utils.paths import segment_dir
from app.web.dependencies import get_config


router = APIRouter(tags=["files"])

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _resolve_under(root: Path, rel_path: str) -> Path:
    """Resolve ``root / rel_path`` and refuse to escape ``root``.

    Rejects absolute paths, ``..`` traversal, symlink escapes, and any
    candidate that doesn't end up underneath the resolved root.
    """
    rel = Path(rel_path)
    if rel.is_absolute() or any(p == ".." for p in rel.parts):
        raise HTTPException(status_code=400, detail="invalid path")
    try:
        candidate = (root / rel).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail="file not found") from exc
    root_resolved = root.resolve()
    if root_resolved != candidate and root_resolved not in candidate.parents:
        raise HTTPException(status_code=400, detail="path escapes output root")
    return candidate


@router.get("/files/{rel_path:path}", include_in_schema=False)
def serve_file(
    rel_path: str, cfg: AppConfig = Depends(get_config),
) -> FileResponse:
    """Stream a file from ``output_root``. ``rel_path`` is the URL path
    after ``/files/``; ``..``  and absolute paths are rejected."""
    path = _resolve_under(Path(cfg.output_root), rel_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not a regular file")
    return FileResponse(path)


def _task_segment_dir(cfg: AppConfig, task_id: str) -> Optional[Path]:
    """Look up the segment dir for ``task_id`` from the tasks row + the
    first task_results path. Returns None if the task or its segment is
    not knowable yet."""
    conn = connect(cfg.db_path, check_same_thread=False)
    try:
        record = get_task(conn, task_id)
        if record is None:
            return None
        # Use the first persisted result's parent if available — that's
        # the authoritative on-disk segment dir. Falls back to the
        # canonical computed path so "open folder" still works for tasks
        # with no downloads yet (operator will see an empty folder).
        row = conn.execute(
            "SELECT video_file_path FROM task_results "
            "WHERE task_id = ? ORDER BY id ASC LIMIT 1",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is not None:
        parent = Path(row["video_file_path"]).parent
        if parent.exists():
            return parent
    # Fall back to today's canonical layout — segment_dir validates
    # components, but "today" is just an approximation for the
    # not-yet-downloaded case.
    from datetime import date as _date
    try:
        return segment_dir(
            cfg.output_root, _date.today(),
            record.sku_id, record.creative_id, record.segment_id,
        )
    except ValueError:
        return None


@router.post("/api/tasks/{task_id}/open-folder",
             status_code=status.HTTP_204_NO_CONTENT)
def open_folder(
    task_id: str, cfg: AppConfig = Depends(get_config),
) -> None:
    """Open the OS file manager pointed at the task's segment dir.

    macOS uses ``open``, Windows ``os.startfile``, Linux ``xdg-open``.
    Bound to localhost so the customer's click is the only trigger.
    """
    folder = _task_segment_dir(cfg, task_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="task or segment not found")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        elif sys.platform == "win32":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"failed to open folder: {exc}",
        ) from exc


def _read_worker_log_tail(
    log_root: Path, workstation_id: str, task_id: str, max_lines: int = 200,
) -> str:
    """Read the worker's log file, return at most ``max_lines`` lines that
    mention this task. Empty string if the file doesn't exist yet."""
    log_path = log_root / f"worker_{workstation_id}.log"
    if not log_path.exists():
        return ""
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    matched = [ln for ln in lines if task_id in ln]
    return "".join(matched[-max_lines:])


@router.get("/tasks/{task_id}/videos/partial",
            response_class=HTMLResponse, include_in_schema=False)
def task_videos_partial(
    task_id: str,
    request: Request,
    cfg: AppConfig = Depends(get_config),
) -> HTMLResponse:
    """Re-render the video grid. Client-side JS swaps it into the page
    only when the tile count grew, so existing <video> elements keep
    their playback state across polls."""
    from app.web.routes.ws import _task_results_for_render
    conn = connect(cfg.db_path, check_same_thread=False)
    try:
        record = get_task(conn, task_id)
        if record is None:
            raise HTTPException(status_code=404)
        results = _task_results_for_render(conn, task_id, Path(cfg.output_root))
    finally:
        conn.close()
    return _templates.TemplateResponse(
        request, "_task_detail_videos.html",
        {"task": record, "results": results},
    )


@router.get("/tasks/{task_id}/log/partial",
            response_class=PlainTextResponse, include_in_schema=False)
def task_log_partial(
    task_id: str, cfg: AppConfig = Depends(get_config),
) -> PlainTextResponse:
    """Return the tail of the worker log for the workstation this task
    is currently / was last assigned to, filtered to lines mentioning
    ``task_id``. Empty body when the task has no assignment yet."""
    conn = connect(cfg.db_path, check_same_thread=False)
    try:
        record = get_task(conn, task_id)
    finally:
        conn.close()
    if record is None:
        raise HTTPException(status_code=404)
    if not record.assigned_workstation_id:
        return PlainTextResponse(content="(not yet assigned)")
    body = _read_worker_log_tail(
        Path(cfg.log_root), record.assigned_workstation_id, task_id,
    )
    return PlainTextResponse(content=body or "(no log lines yet)")

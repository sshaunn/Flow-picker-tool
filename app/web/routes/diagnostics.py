"""Diagnostic bundle endpoints — the operator clicks "下载诊断包" on
the dashboard and gets a zip they can attach to a support email.

Customer-side debugging used to mean "please send me your app.log,
your screenshots, and your DB" three separate times. With one zip
the round trip collapses.

The endpoint lives outside the rest of the JSON API namespace
because the response is a binary download, not JSON.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from app import diagnostics
from app import paths as app_paths


_LOG = logging.getLogger("flow_harvester.diagnostics.route")
router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


@router.post("/build")
def build_bundle() -> JSONResponse:
    """Build a fresh diagnostic zip and return its filename. Operator
    polls / clicks the download endpoint next."""
    try:
        path = diagnostics.build_diagnostic_bundle()
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("diagnostic bundle build failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"诊断包构建失败：{type(exc).__name__}: {exc}",
        ) from exc
    return JSONResponse({
        "filename": path.name,
        "size": path.stat().st_size,
        "download_url": f"/api/diagnostics/download/{path.name}",
    })


@router.get("/download/{filename}")
def download_bundle(filename: str) -> FileResponse:
    """Stream a previously-built bundle. Path-component sanitised so
    the operator can't ../-out of logs_dir."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not filename.startswith("diagnostic_") or not filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="invalid filename")
    target = app_paths.logs_dir() / filename
    if not target.is_file():
        raise HTTPException(status_code=404, detail="bundle not found")
    return FileResponse(
        path=str(target),
        media_type="application/zip",
        filename=filename,
    )

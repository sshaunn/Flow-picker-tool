"""Workstation JSON API.

Backed by ``app.workstations.repository`` — Web UI form posts here, the
same code path the CLI ``workstation add/update/delete`` commands use.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import paths as app_paths
from app.config.loader import FlowModeSpec, WorkstationConfig
from app.web.dependencies import get_db_conn
from app.workstations.repository import (
    WorkstationConflictError,
    WorkstationNotFoundError,
    create_workstation,
    delete_workstation,
    get_workstation,
    list_workstations,
    update_workstation_config,
)


router = APIRouter(prefix="/api/workstations", tags=["workstations"])


class FlowModeIn(BaseModel):
    """Inbound flow_mode payload — None fields leave the Flow UI untouched."""
    tab: Optional[str] = None
    subtab: Optional[str] = None
    aspect: Optional[str] = None
    output_count: Optional[int] = None
    duration_sec: Optional[int] = None
    model: Optional[str] = None


class WorkstationCreate(BaseModel):
    id: str = Field(..., min_length=1, description="e.g. WS_A")
    account_label: str = Field(..., min_length=1)
    browser_profile_path: Optional[str] = Field(
        None,
        description="Defaults to platform paths.workstation_profile_path(id)",
    )
    daily_task_limit: int = Field(20, gt=0)
    flow_project_url: Optional[str] = None
    flow_mode: Optional[FlowModeIn] = None


class WorkstationUpdate(BaseModel):
    """All fields optional — partial PATCH."""
    account_label: Optional[str] = None
    browser_profile_path: Optional[str] = None
    daily_task_limit: Optional[int] = Field(None, gt=0)
    flow_project_url: Optional[str] = None
    flow_mode: Optional[FlowModeIn] = None


class WorkstationOut(BaseModel):
    id: str
    account_label: str
    browser_profile_path: str
    daily_task_limit: int
    status: str
    flow_project_url: Optional[str]
    flow_mode: Optional[FlowModeIn]


def _to_out(ws: WorkstationConfig) -> WorkstationOut:
    return WorkstationOut(
        id=ws.id,
        account_label=ws.account_label,
        browser_profile_path=ws.browser_profile_path,
        daily_task_limit=ws.daily_task_limit,
        status=ws.status,
        flow_project_url=ws.flow_project_url,
        flow_mode=FlowModeIn(**ws.flow_mode.model_dump()) if ws.flow_mode else None,
    )


def _to_flow_mode_spec(payload: Optional[FlowModeIn]) -> Optional[FlowModeSpec]:
    if payload is None:
        return None
    if all(getattr(payload, k) is None for k in FlowModeIn.model_fields):
        return None
    return FlowModeSpec(**payload.model_dump())


@router.get("", response_model=list[WorkstationOut])
def list_route(conn: sqlite3.Connection = Depends(get_db_conn)) -> list[WorkstationOut]:
    return [_to_out(w) for w in list_workstations(conn)]


@router.get("/{ws_id}", response_model=WorkstationOut)
def get_route(ws_id: str, conn: sqlite3.Connection = Depends(get_db_conn)) -> WorkstationOut:
    found = get_workstation(conn, ws_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"workstation not found: {ws_id}")
    return _to_out(found)


@router.post("", response_model=WorkstationOut, status_code=status.HTTP_201_CREATED)
def create_route(
    payload: WorkstationCreate,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> WorkstationOut:
    profile_path = payload.browser_profile_path
    if profile_path is None:
        profile_path = str(app_paths.workstation_profile_path(payload.id))

    try:
        ws = WorkstationConfig(
            id=payload.id,
            account_label=payload.account_label,
            browser_profile_path=profile_path,
            daily_task_limit=payload.daily_task_limit,
            flow_project_url=payload.flow_project_url,
            flow_mode=_to_flow_mode_spec(payload.flow_mode),
        )
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError, etc.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        create_workstation(conn, ws)
    except WorkstationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Materialize the profile dir so the customer's first login can land
    # somewhere — patchright will create it if missing too, but doing it
    # here gives the operator something to inspect immediately.
    from pathlib import Path
    Path(profile_path).mkdir(parents=True, exist_ok=True)

    refreshed = get_workstation(conn, payload.id)
    assert refreshed is not None
    return _to_out(refreshed)


@router.patch("/{ws_id}", response_model=WorkstationOut)
def update_route(
    ws_id: str,
    payload: WorkstationUpdate,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> WorkstationOut:
    fields: dict = {}
    if payload.account_label is not None:
        fields["account_label"] = payload.account_label
    if payload.browser_profile_path is not None:
        fields["browser_profile_path"] = payload.browser_profile_path
    if payload.daily_task_limit is not None:
        fields["daily_task_limit"] = payload.daily_task_limit
    if payload.flow_project_url is not None:
        # Sentinel "" maps to NULL so the UI can clear the field.
        fields["flow_project_url"] = None if payload.flow_project_url == "" else payload.flow_project_url
    if payload.flow_mode is not None:
        fields["flow_mode"] = _to_flow_mode_spec(payload.flow_mode)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")

    try:
        update_workstation_config(conn, ws_id, **fields)
    except WorkstationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    refreshed = get_workstation(conn, ws_id)
    assert refreshed is not None
    return _to_out(refreshed)


@router.delete("/{ws_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_route(ws_id: str, conn: sqlite3.Connection = Depends(get_db_conn)) -> None:
    if not delete_workstation(conn, ws_id):
        raise HTTPException(status_code=404, detail=f"workstation not found: {ws_id}")

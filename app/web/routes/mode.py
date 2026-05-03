"""Operation-mode toggle API.

The customer flips between day (supervised) and night (unattended) from
the top-nav switch. Mode is persisted in ``app_state`` and the daemon
re-reads it at the start of every pass — change takes effect on the
next idle-poll cycle without a server restart.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.state import OperationMode, get_operation_mode, set_operation_mode
from app.web.dependencies import get_db_conn


router = APIRouter(prefix="/api/mode", tags=["mode"])


class ModeOut(BaseModel):
    value: Literal["day", "night"]


class ModeIn(BaseModel):
    value: Literal["day", "night"]


@router.get("", response_model=ModeOut)
def read_mode(conn: sqlite3.Connection = Depends(get_db_conn)) -> ModeOut:
    return ModeOut(value=get_operation_mode(conn).value)


@router.post("", response_model=ModeOut)
def write_mode(
    payload: ModeIn,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> ModeOut:
    try:
        new_mode = set_operation_mode(conn, OperationMode(payload.value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ModeOut(value=new_mode.value)

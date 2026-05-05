"""Per-workstation patchright login session API + HTML partial.

The customer's "Login & detect project" flow:

  POST /api/workstations/{id}/login          → starts a session
  GET  /api/workstations/{id}/login           → JSON status
  DELETE /api/workstations/{id}/login         → cancel
  GET  /workstations/{id}/login/partial       → HTML status block (HTMX)

The session itself lives in ``app.workstations.login_session``. This
module just brokers between HTTP and the registry, plus owns the
on_capture callback that writes the captured project URL + sane mode
preset defaults back to the DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config.loader import AppConfig, FlowModeSpec
from app.db.connection import connect, transaction
from app.web.dependencies import get_config, get_db_conn
from app.workstations.login_session import (
    LoginSession,
    LoginSessionRegistry,
    LoginSnapshot,
    LoginState,
)
from app.workstations.repository import (
    get_workstation,
    soft_revive_workstation,
    update_workstation_config,
)


router = APIRouter(tags=["login"])

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# Sane defaults applied when capture lands a workstation that has no
# flow_mode preset yet. Matches the prior yaml dev configuration —
# Veo 3.1 Fast, 9:16, 1 output, 8s.
_DEFAULT_FLOW_MODE = FlowModeSpec(
    tab="video",
    subtab="ingredients",
    aspect="9:16",
    output_count=1,
    duration_sec=8,
    model="Veo 3.1 - Fast",
)


def _registry(request: Request) -> LoginSessionRegistry:
    return request.app.state.login_sessions


class LoginStatusOut(BaseModel):
    state: str
    captured_url: Optional[str]
    error: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]


def _to_out(snap: LoginSnapshot) -> LoginStatusOut:
    return LoginStatusOut(
        state=snap.state.value,
        captured_url=snap.captured_url,
        error=snap.error,
        started_at=snap.started_at,
        finished_at=snap.finished_at,
    )


def _make_capture_callback(db_path: str, ws_id: str):
    """Build a callback that runs in the patchright thread once a project
    URL is detected — persists the URL + default mode preset to the DB.
    """
    def _on_capture(project_url: str) -> None:
        conn = connect(db_path, check_same_thread=False)
        try:
            ws = get_workstation(conn, ws_id)
            if ws is None:
                return
            fields: dict = {"flow_project_url": project_url}
            # Don't overwrite an existing preset — the operator may have
            # tweaked it on the Edit form already.
            if ws.flow_mode is None:
                fields["flow_mode"] = _DEFAULT_FLOW_MODE
            update_workstation_config(conn, ws_id, **fields)
            # A successful re-login proves the account is reachable, but
            # NOT that patchright is unblocked — Google's anti-bot is at
            # the automation-fingerprint layer, not the session layer.
            # So we only soft-revive: clear cooldown timers and flip
            # cooldown/busy → healthy. Strike counter is preserved so a
            # WS that has been repeatedly tripping anti-bot keeps its
            # accumulated pressure visible. ``manual_check`` and
            # ``nurturing`` rows stay put — those need an operator
            # decision via the explicit transition buttons.
            soft_revive_workstation(conn, ws_id)
        finally:
            conn.close()
    return _on_capture


def _make_no_access_callback(db_path: str, ws_id: str):
    """Build a callback that fires when the login session sees Flow's
    'no access' landing page. Flips the workstation row to
    ``manual_check`` with reason ``no_flow_access`` so the dashboard
    and account tab immediately reflect the dead account.

    Without this the WS would stay at its previous status (healthy /
    cooldown) while the login session itself sits in ERROR — a
    misleading split where the operator sees the red error in the
    login card but green/amber on the WS card.
    """
    def _on_no_access() -> None:
        conn = connect(db_path, check_same_thread=False)
        try:
            with transaction(conn):
                conn.execute(
                    """
                    UPDATE workstations
                       SET status = 'manual_check',
                           cooldown_reason = 'no_flow_access',
                           cooldown_until = NULL,
                           updated_at = datetime('now')
                     WHERE id = ?
                    """,
                    (ws_id,),
                )
        finally:
            conn.close()
    return _on_no_access


@router.post("/api/workstations/{ws_id}/login", response_model=LoginStatusOut,
             status_code=status.HTTP_202_ACCEPTED)
def start_login(
    ws_id: str,
    request: Request,
    cfg: AppConfig = Depends(get_config),
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> LoginStatusOut:
    ws = get_workstation(conn, ws_id)
    if ws is None:
        raise HTTPException(status_code=404, detail=f"workstation not found: {ws_id}")

    registry = _registry(request)
    existing = registry.get(ws_id)
    if existing and existing.is_running:
        return _to_out(existing.status())

    session = LoginSession(
        workstation_id=ws_id,
        profile_path=Path(ws.browser_profile_path),
        entry_url=cfg.flow.entry_url,
        on_capture=_make_capture_callback(cfg.db_path, ws_id),
        on_no_access=_make_no_access_callback(cfg.db_path, ws_id),
    )
    registry.put(session)
    session.start()
    return _to_out(session.status())


@router.get("/api/workstations/{ws_id}/login", response_model=LoginStatusOut)
def login_status(ws_id: str, request: Request) -> LoginStatusOut:
    session = _registry(request).get(ws_id)
    if session is None:
        return LoginStatusOut(
            state=LoginState.NOT_STARTED.value,
            captured_url=None, error=None,
            started_at=None, finished_at=None,
        )
    return _to_out(session.status())


@router.delete("/api/workstations/{ws_id}/login", response_model=LoginStatusOut)
def cancel_login(ws_id: str, request: Request) -> LoginStatusOut:
    session = _registry(request).get(ws_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no login session")
    session.cancel(timeout=5.0)
    return _to_out(session.status())


@router.get("/workstations/{ws_id}/login/partial", response_class=HTMLResponse,
            include_in_schema=False)
def login_partial(
    ws_id: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db_conn),
) -> HTMLResponse:
    """HTMX poll target — small status block re-rendered every second."""
    if get_workstation(conn, ws_id) is None:
        raise HTTPException(status_code=404)
    session = _registry(request).get(ws_id)
    snap = session.status() if session else LoginSnapshot(state=LoginState.NOT_STARTED)
    return _templates.TemplateResponse(
        request,
        "_login_partial.html",
        {"ws_id": ws_id, "snap": snap, "states": LoginState},
    )

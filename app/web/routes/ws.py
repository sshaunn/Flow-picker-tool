"""WebSocket endpoints for live dashboard updates.

The push payload is a pre-rendered HTML fragment (the dashboard inner
grid) so the client only has to swap ``innerHTML`` once per tick. That
keeps templating server-side and avoids a separate JSON-render path.

Update strategy is server-polled (re-render every ``push_interval_sec``)
rather than event-driven; the daemon and the runner already update DB
state, so a periodic snapshot stays simple and bounded. Future work
could swap this for a pub/sub feed off ``finalize_task`` if 1-2s latency
turns out to be too coarse.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.templating import Jinja2Templates

from app.db.connection import connect
from app.tasks.repository import list_tasks
from app.workstations.repository import list_workstations


router = APIRouter(tags=["ws"], include_in_schema=False)


_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _render_dashboard(app) -> str:
    """Render the dashboard grid partial against the current DB / daemon state.

    Called from inside a WebSocket handler where there is no HTTP Request
    object — the partial doesn't reference ``request``/``url_for``, so we
    render the template directly with just the data it needs.
    """
    cfg = app.state.config
    daemon = app.state.daemon
    snap = daemon.status()
    daemon_dict = {
        "running": snap.running,
        "rounds_completed": snap.rounds_completed,
        "last_round_at": snap.last_round_at,
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
    conn = connect(cfg.db_path, check_same_thread=False)
    try:
        ws_list = list_workstations(conn)
        tasks = list_tasks(conn, limit=10)
    finally:
        conn.close()
    template = _templates.get_template("_dashboard_grid.html")
    return template.render(
        daemon=daemon_dict,
        workstations=ws_list,
        tasks=tasks,
    )


@router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    """Push a fresh dashboard fragment every ``push_interval_sec``.

    Pulls ``push_interval_sec`` (default 2.0) from ``app.state`` so tests
    can crank it down to milliseconds without monkey-patching.
    """
    await websocket.accept()
    app = websocket.app
    interval = getattr(app.state, "push_interval_sec", 2.0)
    try:
        while True:
            await websocket.send_text(_render_dashboard(app))
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        return

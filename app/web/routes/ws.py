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

from pathlib import Path as _PathlibPath

from app.db.connection import connect
from app.tasks.repository import get_task, get_task_assets, list_tasks
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


def _task_results_for_render(conn, task_id: str, output_root: _PathlibPath) -> list[dict]:
    """Convert ``task_results`` rows into the shape the partial template
    expects: ``{round, seq, rel_path}``. Paths are made relative to
    ``output_root`` so the /files route can resolve them safely."""
    rows = conn.execute(
        "SELECT generation_round, sequence_no, video_file_path "
        "FROM task_results WHERE task_id = ? "
        "ORDER BY generation_round ASC, sequence_no ASC",
        (task_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            rel = _PathlibPath(r["video_file_path"]).resolve().relative_to(
                output_root.resolve()
            )
        except (ValueError, OSError):
            continue
        out.append({
            "round": r["generation_round"],
            "seq": r["sequence_no"],
            "rel_path": str(rel),
        })
    return out


def _render_task_status(app, task_id: str) -> str | None:
    """Render only the status / progress block — fast-changing data the
    WebSocket pushes every tick. Excludes the <video> grid so a clip the
    customer is previewing isn't torn down by the swap."""
    cfg = app.state.config
    conn = connect(cfg.db_path, check_same_thread=False)
    try:
        record = get_task(conn, task_id)
        if record is None:
            return None
    finally:
        conn.close()
    template = _templates.get_template("_task_detail_status.html")
    return template.render(task=record)


@router.websocket("/ws/task/{task_id}")
async def task_detail_ws(websocket: WebSocket, task_id: str) -> None:
    """Push the task detail inner-grid every ``push_interval_sec``.

    Closes the socket if the task disappears (deleted by the operator)
    so the client's reconnect loop does the right thing.
    """
    await websocket.accept()
    app = websocket.app
    interval = getattr(app.state, "push_interval_sec", 2.0)
    try:
        while True:
            html = _render_task_status(app, task_id)
            if html is None:
                await websocket.close()
                return
            await websocket.send_text(html)
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        return

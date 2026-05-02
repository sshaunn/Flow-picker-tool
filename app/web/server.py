"""FastAPI app factory + lifespan.

The lifespan hook auto-starts the scheduler daemon on server boot and
shuts it down cleanly on exit so the customer doesn't need to click
"Start" — the moment they double-click the desktop shortcut, the loop
is running. Workstation list comes from the DB (the canonical source);
if the DB is empty, the server still boots and the daemon idles until
the customer adds at least one WS via the form.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import paths as app_paths
from app.config.loader import AppConfig
from app.db.connection import connect
from app.db.schema import init_schema
from app.scheduler.daemon import SchedulerDaemon
from app.web.routes import scheduler as scheduler_routes
from app.web.routes import tasks as tasks_routes
from app.web.routes import workstations as ws_routes
from app.workstations.repository import list_workstations


_DASHBOARD_PLACEHOLDER = """\
<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>Flow Harvester</title></head>
<body style='font-family: ui-sans-serif, system-ui; padding: 2rem;'>
  <h1>Flow Harvester</h1>
  <p>Server is running. The dashboard UI is rendered from the API endpoints below.</p>
  <ul>
    <li><a href='/api/workstations'>GET /api/workstations</a></li>
    <li><a href='/api/tasks'>GET /api/tasks</a></li>
    <li><a href='/api/scheduler/status'>GET /api/scheduler/status</a></li>
    <li><a href='/docs'>OpenAPI / Swagger</a></li>
  </ul>
</body></html>
"""


def _make_lifespan(*, auto_start_daemon: bool):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log = logging.getLogger("flow_harvester.web")
        cfg: AppConfig = app.state.config

        # Make sure the DB exists and the schema is up to date — fresh
        # customer install boots straight from the .bat into a working
        # state without any manual init step.
        init_schema(Path(cfg.db_path))
        app_paths.ensure_app_dirs()

        # Pull workstations from DB at startup. If empty, daemon idles
        # until the customer adds one via POST /api/workstations.
        with connect(cfg.db_path) as conn:
            ws_list = list_workstations(conn)

        daemon = SchedulerDaemon(
            db_path=cfg.db_path,
            config=cfg,
            workstations=ws_list,
            idle_poll_sec=app.state.idle_poll_sec,
            use_mock=app.state.use_mock,
            mock_round_plans_per_ws=app.state.mock_round_plans_per_ws,
        )
        app.state.daemon = daemon

        if auto_start_daemon and ws_list:
            daemon.start()
            log.info("scheduler daemon started with %d workstation(s)", len(ws_list))
        elif not ws_list:
            log.info("no workstations in DB; scheduler idle until POST /api/workstations")
        try:
            yield
        finally:
            log.info("shutting down scheduler daemon")
            daemon.stop(timeout=30.0)

    return lifespan


def create_app(
    *,
    config: AppConfig,
    auto_start_daemon: bool = True,
    idle_poll_sec: float = 5.0,
    use_mock: bool = False,
    mock_round_plans_per_ws: Optional[dict] = None,
) -> FastAPI:
    """Build a FastAPI app instance with the scheduler daemon wired in.

    ``use_mock`` / ``mock_round_plans_per_ws`` exist for tests — the real
    customer install always uses the patchright-backed FlowPort.
    """
    app = FastAPI(
        title="Flow Harvester",
        description="Local Web UI for Flow video harvesting (V1).",
        version="0.1.0",
        lifespan=_make_lifespan(auto_start_daemon=auto_start_daemon),
    )
    app.state.config = config
    app.state.idle_poll_sec = idle_poll_sec
    app.state.use_mock = use_mock
    app.state.mock_round_plans_per_ws = mock_round_plans_per_ws

    app.include_router(ws_routes.router)
    app.include_router(tasks_routes.router)
    app.include_router(scheduler_routes.router)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return _DASHBOARD_PLACEHOLDER

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    return app

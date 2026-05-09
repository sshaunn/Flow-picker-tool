"""FastAPI app factory + lifespan.

The lifespan hook auto-starts the scheduler daemon on server boot and
shuts it down cleanly on exit so the customer doesn't need to click
"Start" — the moment they double-click the desktop shortcut, the loop
is running. Workstation list comes from the DB (the canonical source);
if the DB is empty, the server still boots and the daemon idles until
the customer adds at least one WS via the form.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import paths as app_paths
from app.config.loader import AppConfig
from app.db.connection import connect
from app.db.schema import init_schema
from app.scheduler.daemon import SchedulerDaemon
from app.web.routes import diagnostics as diagnostics_routes
from app.web.routes import files as files_routes
from app.web.routes import login as login_routes
from app.web.routes import mode as mode_routes
from app.web.routes import tunnel as tunnel_routes
from app.web.routes import pages as page_routes
from app.web.routes import scheduler as scheduler_routes
from app.web.routes import tasks as tasks_routes
from app.web.routes import workstations as ws_routes
from app.web.routes import ws as ws_routes_module
from app.workstations.login_session import LoginSessionRegistry
from app.workstations.repository import list_workstations


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

        # Process-boot zombie cleanup: any tasks still ``running`` from
        # the previous process are by definition orphaned (no in-flight
        # worker exists in this fresh process). The mid-loop
        # recover_zombie_tasks would only act on rows older than
        # running_stale_minutes — that's fine for live crashes but
        # leaves the customer UI stuck for several minutes after every
        # restart, and customers run the bundled exe locally with no
        # central server / no DB shell to hand-edit. Reset eagerly here.
        from app.db.connection import connect as _connect
        from app.scheduler.recovery import reset_zombie_state_on_startup
        with _connect(cfg.db_path) as cleanup_conn:
            cleanup_summary = reset_zombie_state_on_startup(cleanup_conn)
        if cleanup_summary.revived or cleanup_summary.escalated_manual:
            log.info(
                "startup zombie cleanup: revived=%d escalated_manual=%d",
                cleanup_summary.revived, cleanup_summary.escalated_manual,
            )

        # Daemon re-queries workstations from DB each pass (so WS added
        # via the Web UI after boot are picked up automatically).
        daemon = SchedulerDaemon(
            db_path=cfg.db_path,
            config=cfg,
            idle_poll_sec=app.state.idle_poll_sec,
            use_mock=app.state.use_mock,
            mock_round_plans_per_ws=app.state.mock_round_plans_per_ws,
        )
        app.state.daemon = daemon

        with connect(cfg.db_path) as conn:
            ws_count = len(list_workstations(conn))

        if auto_start_daemon:
            daemon.start()
            log.info("scheduler daemon started; %d workstation(s) currently in DB",
                     ws_count)
        elif ws_count == 0:
            log.info("no workstations in DB; scheduler idle until POST /api/workstations")

        # V2 spike: hand the running asyncio loop to ExtensionFlowPort so
        # worker threads can bridge into the WS dispatcher via
        # run_coroutine_threadsafe. Only takes effect when both spike
        # flags are set; without them V1 production paths run unchanged.
        import os as _os
        spike_mounted = _os.environ.get("FLOW_HARVESTER_SPIKE_EXTENSION") == "1"
        use_extension = _os.environ.get("FLOW_HARVESTER_USE_EXTENSION") == "1"
        log.info(
            "[spike] flags: FLOW_HARVESTER_SPIKE_EXTENSION=%s FLOW_HARVESTER_USE_EXTENSION=%s",
            "1" if spike_mounted else "0 (NOT SET)",
            "1" if use_extension else "0 (NOT SET)",
        )
        if use_extension and not spike_mounted:
            log.warning(
                "[spike] FLOW_HARVESTER_USE_EXTENSION is on but FLOW_HARVESTER_SPIKE_EXTENSION is OFF "
                "— the WS endpoint /ws/extension/{ws_id} is NOT mounted, so the extension can't "
                "register and every RPC will fail with 'workstation X not connected'. Set both to 1."
            )
        if spike_mounted and use_extension:
            from app.worker.flow_extension_port import set_runtime_loop as _set_loop
            _set_loop(asyncio.get_running_loop())
            log.info("[spike] ExtensionFlowPort runtime loop registered")

        # V2 spike file logging — V1's ``app/utils/logging.py`` only
        # attaches FileHandlers to ``flow_harvester.scheduler`` and
        # ``flow_harvester.worker.<id>``. Spike-only loggers
        # (``flow_harvester.spike.*`` / ``flow_harvester.worker.extension_port``)
        # would otherwise only land on stderr — which I can't see when
        # diagnosing the customer's run after the fact. Funnel them to
        # ``logs/spike-extension.log`` whenever either spike flag is on.
        if spike_mounted or use_extension:
            spike_log_path = app_paths.logs_dir() / "spike-extension.log"
            spike_log_path.parent.mkdir(parents=True, exist_ok=True)
            spike_handler = logging.FileHandler(str(spike_log_path), encoding="utf-8")
            spike_handler.setLevel(logging.DEBUG)
            spike_handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            )
            spike_handler._flow_harvester_spike = True  # type: ignore[attr-defined]
            # Attach to parent loggers only — children (e.g.
            # ``flow_harvester.spike.extension_ws``) propagate up so they
            # don't need their own handler. Adding to both parent and
            # child duplicates every log line, which is what the early
            # spike-extension.log was suffering from.
            for name in (
                "flow_harvester.spike",
                "flow_harvester.worker.extension_port",
                "flow_harvester.web",
            ):
                lg = logging.getLogger(name)
                # Idempotent: don't double-attach on multiple lifespan boots.
                if not any(getattr(h, "_flow_harvester_spike", False) for h in lg.handlers):
                    lg.addHandler(spike_handler)
                    lg.setLevel(logging.DEBUG)
            log.info("[spike] file logger → %s", spike_log_path)

        try:
            yield
        finally:
            log.info("shutting down scheduler daemon")
            daemon.stop(timeout=30.0)
            # Make sure any in-flight login Chrome windows close with us.
            registry: LoginSessionRegistry = app.state.login_sessions
            registry.cancel_all(timeout=5.0)

    return lifespan


def create_app(
    *,
    config: AppConfig,
    auto_start_daemon: bool = True,
    idle_poll_sec: float = 5.0,
    push_interval_sec: float = 2.0,
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
    app.state.push_interval_sec = push_interval_sec
    app.state.use_mock = use_mock
    app.state.mock_round_plans_per_ws = mock_round_plans_per_ws
    app.state.login_sessions = LoginSessionRegistry()
    # Tunnel manager: lazy-initialised. The port is set by the caller
    # (``__main__`` or ``cli serve``) after it picks a free port. The
    # dashboard "开启远程调试" button drives this manager.
    from app.tunnel import TunnelManager
    app.state.tunnel_manager = TunnelManager(port=0)
    app.state.bound_port = 0

    app.include_router(ws_routes.router)
    app.include_router(tasks_routes.router)
    app.include_router(scheduler_routes.router)
    app.include_router(page_routes.router)
    app.include_router(ws_routes_module.router)
    app.include_router(login_routes.router)
    app.include_router(files_routes.router)
    app.include_router(mode_routes.router)
    app.include_router(diagnostics_routes.router)
    app.include_router(tunnel_routes.router)

    # V2 extension spike — only mounted when explicitly enabled. Default
    # off so V1 production behaviour is bit-identical.
    import os as _os
    if _os.environ.get("FLOW_HARVESTER_SPIKE_EXTENSION") == "1":
        from app.web.routes import extension_ws as _extension_ws_routes
        app.include_router(_extension_ws_routes.router)
        logging.getLogger("flow_harvester.web").info(
            "[spike] V2 extension WS routes mounted (FLOW_HARVESTER_SPIKE_EXTENSION=1)"
        )

    # Make the friendly-error helpers callable from any Jinja2 template.
    from app.web import messages as _messages
    page_routes.templates.env.globals["task_error_friendly"] = _messages.task_error_friendly
    page_routes.templates.env.globals["ws_cooldown_friendly"] = _messages.ws_cooldown_friendly
    ws_routes_module._templates.env.globals["task_error_friendly"] = _messages.task_error_friendly
    ws_routes_module._templates.env.globals["ws_cooldown_friendly"] = _messages.ws_cooldown_friendly
    login_routes._templates.env.globals["ws_cooldown_friendly"] = _messages.ws_cooldown_friendly
    files_routes._templates.env.globals["task_error_friendly"] = _messages.task_error_friendly

    # Expose the current operation mode to every base.html render so the
    # top-nav toggle paints the active button server-side (no flash).
    from app.state import get_operation_mode

    def _current_mode() -> str:
        try:
            with connect(config.db_path) as conn:
                return get_operation_mode(conn).value
        except Exception:  # noqa: BLE001 — fail soft, default day
            return "day"

    for tpl_env in (page_routes.templates.env, ws_routes_module._templates.env,
                    login_routes._templates.env, files_routes._templates.env):
        tpl_env.globals["current_mode"] = _current_mode

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    return app

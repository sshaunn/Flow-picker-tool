"""Native desktop entry point for the bundled customer install.

Goal: customer double-clicks ``FlowHarvester.exe`` and gets a single
native window — no console, no separate browser tab. Internally we run
the FastAPI server on a background thread and wrap it in pywebview,
which uses Edge WebView2 on Windows 10/11 (already installed) and
WKWebView on macOS.

Lifecycle:

  startup  → uvicorn.Server boots on a daemon thread → wait for /healthz
  display  → pywebview opens a window pointing at http://127.0.0.1:<port>/
  shutdown → user closes the window → ``server.should_exit = True``,
             daemon thread joins, FastAPI lifespan runs (stops scheduler
             daemon, cancels any in-flight login Chrome window).

If anything goes wrong before the window opens, we fall back to printing
the traceback and pausing for input — better than a silent flash-and-
disappear crash.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


def _resource_root() -> Path:
    """Where bundled data files live. PyInstaller stages them under
    ``sys._MEIPASS`` at runtime; in dev we walk up from this file."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def _pick_free_port(preferred: int) -> int:
    """Return ``preferred`` if available, else the OS-assigned next free
    port. Avoids the customer-side "port 8080 in use" crash in favor of
    just working."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout_sec: float = 30.0) -> bool:
    """Poll the dashboard URL until it answers 200 or we hit timeout."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 — server still starting
            pass
        time.sleep(0.2)
    return False


def _setup_file_logging() -> None:
    """Without a console window, uvicorn / app loggers have no stdout to
    write to. Route them to ``%LOCALAPPDATA%\\FlowHarvester\\logs\\app.log``
    so customers can attach the file when they ping support."""
    from app import paths as app_paths
    log_dir = app_paths.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    ))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access",
                 "flow_harvester", "flow_harvester.scheduler",
                 "flow_harvester.daemon", "flow_harvester.web",
                 "flow_harvester.login"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False


def main() -> None:
    # PyInstaller drops bundled data files alongside the exe; chdir into
    # there so default config / templates resolve correctly.
    os.chdir(_resource_root())

    settings_path = Path("config/settings.yaml")
    if not settings_path.exists():
        settings_path = _resource_root() / "config" / "settings.yaml"

    from app.config.loader import load_settings
    from app.web.server import create_app
    import uvicorn

    _setup_file_logging()

    cfg = load_settings(str(settings_path))
    app = create_app(
        config=cfg,
        auto_start_daemon=True,
        idle_poll_sec=5.0,
    )

    preferred_port = int(os.environ.get("FLOW_HARVESTER_PORT", "8080"))
    port = _pick_free_port(preferred_port)
    url = f"http://127.0.0.1:{port}/"

    server: Optional[uvicorn.Server] = None

    def _run_server() -> None:
        nonlocal server
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port,
            log_level="info", access_log=False,
        )
        server = uvicorn.Server(config)
        server.run()

    server_thread = threading.Thread(target=_run_server, daemon=True,
                                     name="flow-harvester-uvicorn")
    server_thread.start()

    if not _wait_for_server(url + "healthz", timeout_sec=30.0):
        raise RuntimeError(
            f"Server did not respond on {url} within 30 seconds. "
            "Check the log at %LOCALAPPDATA%\\FlowHarvester\\logs\\app.log",
        )

    # Native window — Edge WebView2 on Win10/11, WKWebView on macOS.
    import webview
    webview.create_window(
        title="Flow Harvester",
        url=url,
        width=1280,
        height=820,
        min_size=(960, 600),
        resizable=True,
        confirm_close=False,
    )
    webview.start()

    # Window closed → graceful shutdown.
    if server is not None:
        server.should_exit = True
    server_thread.join(timeout=15.0)


if __name__ == "__main__":
    # Catch and surface errors so a failed bundle launch doesn't just
    # flash a console window and disappear — the customer needs to see
    # the traceback to send to support.
    try:
        main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 — we want everything
        import traceback
        # When console=False there's no stdout. Best effort: write to a
        # crash file next to the logs and pop a message via webview if
        # we got that far, else fall through to a console print which
        # the dev-from-source path will see.
        try:
            from app import paths as app_paths
            crash_path = app_paths.logs_dir() / "crash.log"
            crash_path.parent.mkdir(parents=True, exist_ok=True)
            with crash_path.open("a", encoding="utf-8") as fh:
                fh.write("=" * 60 + "\n")
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + " crash:\n")
                traceback.print_exc(file=fh)
        except Exception:  # noqa: BLE001
            pass
        # Best-effort native dialog so the customer sees something.
        try:
            import webview
            webview.create_window(
                "Flow Harvester crash",
                html=f"""<!DOCTYPE html>
                <html><body style='font-family:sans-serif;padding:2em'>
                <h2>启动失败</h2>
                <p>详细信息已写入：<code>%LOCALAPPDATA%\\FlowHarvester\\logs\\crash.log</code></p>
                <pre style='background:#fee;padding:1em;overflow:auto'>{traceback.format_exc()}</pre>
                </body></html>""",
                width=720, height=540,
            )
            webview.start()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            try:
                input("Press Enter to close...")
            except EOFError:
                pass
        sys.exit(1)

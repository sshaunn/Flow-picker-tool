"""Entry point for the bundled Windows build.

When PyInstaller wraps the app into ``FlowHarvester.exe``, double-clicking
the exe runs ``python -m app`` ⇒ this module. We boot the FastAPI server
on port 8080, open the customer's default browser to the dashboard, and
keep the console window alive so they can see logs / hit Ctrl+C to stop.

Running from source (``python -m app``) does the same thing, so this is
also a friendlier dev launcher than ``flow-harvester serve``.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _resource_root() -> Path:
    """Where bundled data files (``app/web/templates/``, ``config/``)
    live. PyInstaller stages them under ``sys._MEIPASS`` at runtime; in
    dev we just walk up from this file."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def _open_browser_after(url: str, delay_sec: float = 2.5) -> None:
    """Open the dashboard once the server has had time to bind. Runs on
    its own thread so we don't block the uvicorn boot sequence."""
    def _go() -> None:
        time.sleep(delay_sec)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — best effort
            pass
    threading.Thread(target=_go, daemon=True).start()


def main() -> None:
    # PyInstaller drops bundled data files alongside the exe; chdir into
    # there so relative paths inside templates / Jinja2 lookups still
    # resolve correctly.
    os.chdir(_resource_root())

    # Load default config from the bundled YAML if the customer hasn't
    # placed an override beside the exe.
    settings_path = Path("config/settings.yaml")
    if not settings_path.exists():
        # Fallback to whatever shipped inside the bundle.
        settings_path = _resource_root() / "config" / "settings.yaml"

    from app.config.loader import load_settings
    from app.web.server import create_app
    import uvicorn

    cfg = load_settings(str(settings_path))
    app = create_app(
        config=cfg,
        auto_start_daemon=True,
        idle_poll_sec=5.0,
    )

    port = int(os.environ.get("FLOW_HARVESTER_PORT", "8080"))
    _open_browser_after(f"http://127.0.0.1:{port}/")
    print("=" * 60)
    print("  Flow Harvester is running.")
    print(f"  Dashboard: http://127.0.0.1:{port}/")
    print("  Keep this window open. Press Ctrl+C to stop.")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    # Catch and surface errors so a failed bundle launch doesn't just
    # flash a console window and disappear — the customer needs to see
    # the traceback to send it to support.
    try:
        main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 — we want everything
        import traceback
        print("\n" + "=" * 60)
        print("  Flow Harvester crashed.")
        print("=" * 60)
        traceback.print_exc()
        print("=" * 60)
        try:
            input("Press Enter to close this window...")
        except EOFError:
            pass
        sys.exit(1)

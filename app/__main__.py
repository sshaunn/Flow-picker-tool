"""Bundled entry point for the customer install.

Customer double-clicks ``FlowHarvester.exe``, a cmd window opens
showing live logs, and the dashboard auto-opens in their default
browser. Closing the cmd window (or Ctrl+C) shuts everything down.

Lifecycle:

  startup  → resolve bundled yaml paths
           → boot uvicorn in the foreground (blocks the cmd window)
           → after a short delay, ``webbrowser.open`` to the dashboard
  shutdown → user closes cmd window or hits Ctrl+C → uvicorn stops,
             FastAPI lifespan runs (stops scheduler daemon, cancels any
             in-flight login Chrome window).
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _resource_root() -> Path:
    """Where bundled data files live. PyInstaller stages them under
    ``sys._MEIPASS`` at runtime; in dev we walk up from this file."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def _find_bundled_yaml(rel: str) -> Path:
    """Locate a bundled yaml across every PyInstaller layout we've seen
    in the wild. Some PyInstaller builds drop datas into ``_MEIPASS``,
    others into ``_internal/`` next to the exe, so try them all and
    return the first match. Raises with a helpful list if none exist.
    """
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        try:
            r = p.resolve()
        except (OSError, RuntimeError):
            r = p
        if r not in seen:
            seen.add(r)
            candidates.append(p)

    _add(_resource_root() / rel)
    exe_dir = Path(sys.executable).resolve().parent
    _add(exe_dir / rel)
    _add(exe_dir / "_internal" / rel)
    _add(Path.cwd() / rel)

    for path in candidates:
        if path.exists():
            return path

    listing = "\n  - ".join(str(p) for p in candidates)
    raise FileNotFoundError(
        f"Could not locate bundled resource {rel!r}. Looked in:\n  - {listing}",
    )


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


def _setup_file_logging() -> None:
    """Mirror logs to ``%LOCALAPPDATA%\\FlowHarvester\\logs\\app.log``
    in addition to the cmd window, so customers can attach the file when
    they ping support.

    Attach the handler ONLY to the package roots (``flow_harvester``
    and ``uvicorn``); descendants propagate up via Python's logger
    hierarchy. Attaching to both parent and named children would
    duplicate every line in app.log (the customer-side bug we saw —
    daemon-started messages appearing twice in the same millisecond).
    """
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
    # Only attach to the two trees we care about. Their descendants
    # (``flow_harvester.scheduler``, ``flow_harvester.worker.*``,
    # ``uvicorn.error``, ...) will reach this handler via propagation
    # as long as they don't disable propagate themselves.
    for name in ("uvicorn", "flow_harvester"):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def _enforce_license() -> None:
    """Refuse to boot if no valid license.key is present.

    Only checks when running as the bundled exe (``sys.frozen``) so dev
    iteration with ``python -m app`` doesn't need a license. Customers
    can drop ``license.key`` either next to the exe (initial install)
    or under ``%LOCALAPPDATA%\\FlowHarvester\\`` (operator-managed
    persistent location). See ``app.license.find_license_file`` for
    the full search order.

    Raises ``LicenseError`` (handled by the outer crash handler in
    ``__main__``) so the customer sees a friendly Chinese banner +
    crash.log entry instead of a stack trace.
    """
    if not getattr(sys, "frozen", False):
        return
    from app.license import (
        LicenseError, find_license_file, load_license_file,
    )
    path = find_license_file()
    if path is None:
        raise LicenseError(
            "未找到授权文件 license.key — 请联系开发者获取，"
            "把它放在 FlowHarvester.exe 所在目录或 "
            "%LOCALAPPDATA%\\FlowHarvester\\ 下，再启动。"
        )
    payload = load_license_file(path)
    expires_local = payload.expires_at.astimezone().strftime("%Y-%m-%d %H:%M")
    print(
        f"   授权: {payload.customer_id}, "
        f"剩余 {payload.days_remaining} 天 (至 {expires_local})",
        flush=True,
    )
    if payload.days_remaining <= 7:
        print(
            "   ⚠ 授权即将到期，请提前联系开发者续期",
            flush=True,
        )


def main() -> None:
    os.chdir(_resource_root())

    settings_path = _find_bundled_yaml("config/settings.yaml")
    selectors_path = _find_bundled_yaml("config/flow-selectors.yaml")
    os.environ.setdefault(
        "FLOW_HARVESTER_SELECTORS_YAML", str(selectors_path),
    )

    from app.config.loader import load_settings
    from app.web.server import create_app
    import uvicorn

    _setup_file_logging()
    # License check runs after logging is wired so failures land in
    # app.log, but before the server starts so an expired install
    # fails fast without binding a port.
    _enforce_license()

    cfg = load_settings(str(settings_path))
    app = create_app(
        config=cfg,
        auto_start_daemon=True,
        idle_poll_sec=5.0,
    )

    preferred_port = int(os.environ.get("FLOW_HARVESTER_PORT", "8080"))
    port = _pick_free_port(preferred_port)
    url = f"http://127.0.0.1:{port}/"

    # Banner so the customer immediately sees the dashboard URL even if
    # the auto-open below is blocked by their browser settings.
    print("=" * 60)
    print(" Flow Harvester")
    print("=" * 60)
    print(f" Dashboard: {url}")
    print(" Keep this window open while you use the tool.")
    print(" Close this window (or Ctrl+C) to stop.")
    print("=" * 60, flush=True)

    def _open_browser() -> None:
        time.sleep(2.0)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — best-effort
            pass

    threading.Thread(target=_open_browser, daemon=True,
                     name="flow-harvester-open-browser").start()

    config = uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_level="info", access_log=False,
    )
    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — we want everything
        import traceback
        # License errors are expected end-of-trial / install-without-key
        # cases, not crashes. Show the (already-Chinese) message
        # cleanly without a stack trace and skip crash.log noise.
        try:
            from app.license import LicenseError as _LicenseError
        except Exception:  # noqa: BLE001
            _LicenseError = None  # type: ignore[assignment]
        is_license_error = (
            _LicenseError is not None and isinstance(exc, _LicenseError)
        )

        if not is_license_error:
            # Write the traceback to crash.log first so the customer
            # can share the file even if they close the cmd window
            # before reading it.
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

        # cmd window is visible — print the message and pause so the
        # customer can read it / screenshot it before closing.
        print("\n" + "=" * 60, file=sys.stderr)
        if is_license_error:
            print(" Flow Harvester 授权失败", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            print(f" {exc}", file=sys.stderr)
        else:
            print(" Flow Harvester crashed", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            traceback.print_exc()
        try:
            input("\nPress Enter to close this window ...")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)

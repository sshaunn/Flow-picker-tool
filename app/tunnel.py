"""Cloudflare tunnel wrapper for remote diagnostic access.

When the customer is debugging a problem with dev, the dashboard's
"开启远程调试" button starts ``cloudflared tunnel --url
http://localhost:<port>``. Cloudflare prints a fresh public URL
(``https://*.trycloudflare.com``) — operator copies it and sends to
dev. Dev opens it and sees the customer's exact running dashboard,
realtime logs, account state, etc. Stop button kills the subprocess
and the URL stops working immediately.

No Cloudflare account / token required for the free
``trycloudflare.com`` quick-tunnel mode. The binary is shipped
alongside ``FlowHarvester.exe`` (Win build) — the GitHub Actions
workflow downloads it from cloudflare's official releases and copies
it into ``dist\\FlowHarvester\\``. On macOS dev (where the binary may
not be present) the module detects absence gracefully and the
dashboard button surfaces a friendly message.

Security note: the URL is **public** — anyone with it can see the
customer's running dashboard. The button copy warns operator to
share only with dev and stop it once the debug session is over. A
later iteration may add a one-time-password challenge in front of
all routes when the tunnel is active.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_LOG = logging.getLogger("flow_harvester.tunnel")

# Cloudflare prints the public URL to stderr inside a banner box —
# match it loosely so future formatting tweaks don't break detection.
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)

# Time we wait for the URL line to appear after starting the
# subprocess. The quick-tunnel handshake usually completes in 2-5s
# but customer networks behind picky firewalls can take longer.
_URL_WAIT_TIMEOUT_SEC = 30.0


@dataclass
class TunnelStatus:
    running: bool
    public_url: Optional[str]
    error: Optional[str]
    started_at: Optional[str]
    binary_found: bool


def _find_cloudflared() -> Optional[Path]:
    """Locate the cloudflared binary. Search order:

    1. ``cloudflared.exe`` next to the bundled exe (PyInstaller --onedir
       layout — the GitHub Actions workflow drops it there).
    2. ``_internal/cloudflared.exe`` under PyInstaller's _MEIPASS.
    3. The system ``PATH`` (so ``brew install cloudflared`` on macOS
       dev / ``choco install cloudflared`` on Win works for testing).
    """
    name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        candidate = exe_dir / name
        if candidate.is_file():
            return candidate
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate = Path(meipass) / name
            if candidate.is_file():
                return candidate

    # Fall back to PATH lookup.
    from shutil import which
    found = which(name)
    if found:
        return Path(found)
    return None


class TunnelManager:
    """One per process. Idempotent start/stop. Thread-safe enough for
    the FastAPI dashboard's button presses."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._url: Optional[str] = None
        self._error: Optional[str] = None
        self._started_at: Optional[str] = None
        self._reader_thread: Optional[threading.Thread] = None

    def status(self) -> TunnelStatus:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            return TunnelStatus(
                running=running,
                public_url=self._url if running else None,
                error=self._error,
                started_at=self._started_at if running else None,
                binary_found=_find_cloudflared() is not None,
            )

    def start(self) -> TunnelStatus:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return TunnelStatus(
                    running=True,
                    public_url=self._url,
                    error=None,
                    started_at=self._started_at,
                    binary_found=True,
                )
            binary = _find_cloudflared()
            if binary is None:
                self._error = (
                    "未找到 cloudflared 程序。如果你在客户端 Win11 上跑，"
                    "说明 bundle 里漏了 cloudflared.exe；请联系开发重发安装包。"
                )
                _LOG.warning("tunnel start refused: cloudflared not found")
                return TunnelStatus(
                    running=False, public_url=None,
                    error=self._error, started_at=None,
                    binary_found=False,
                )
            self._error = None
            self._url = None
            cmd = [
                str(binary), "tunnel",
                "--url", f"http://localhost:{self.port}",
                "--no-autoupdate",
            ]
            _LOG.info("starting cloudflared tunnel: %s", cmd)
            try:
                # Merge stderr into stdout so we can scan a single
                # stream for the URL banner.
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                        if sys.platform == "win32" else 0
                    ),
                    env={**os.environ},
                )
            except Exception as exc:  # noqa: BLE001
                self._error = f"启动 cloudflared 失败: {exc}"
                _LOG.exception("cloudflared spawn failed: %s", exc)
                return TunnelStatus(
                    running=False, public_url=None,
                    error=self._error, started_at=None,
                    binary_found=True,
                )
            from datetime import datetime, timezone
            self._started_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            self._reader_thread = threading.Thread(
                target=self._read_output, name="cloudflared-reader",
                daemon=True,
            )
            self._reader_thread.start()

        # Wait for URL outside lock so concurrent status() calls work.
        deadline = time.monotonic() + _URL_WAIT_TIMEOUT_SEC
        while time.monotonic() < deadline:
            with self._lock:
                if self._url is not None:
                    return TunnelStatus(
                        running=True, public_url=self._url,
                        error=None, started_at=self._started_at,
                        binary_found=True,
                    )
                if self._proc is not None and self._proc.poll() is not None:
                    self._error = (
                        f"cloudflared 提前退出 (exit={self._proc.returncode})。"
                        "可能是网络问题或 cloudflared 版本不兼容。"
                    )
                    return TunnelStatus(
                        running=False, public_url=None,
                        error=self._error, started_at=None,
                        binary_found=True,
                    )
            time.sleep(0.2)

        # Timeout: process is alive but no URL yet. Give status anyway.
        with self._lock:
            return TunnelStatus(
                running=self._proc is not None and self._proc.poll() is None,
                public_url=self._url,
                error="cloudflared 启动超时（30 秒未拿到 URL），可能网络受限。",
                started_at=self._started_at,
                binary_found=True,
            )

    def stop(self) -> TunnelStatus:
        with self._lock:
            proc = self._proc
            self._proc = None
            self._url = None
            self._started_at = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("cloudflared terminate raised: %s", exc)
        return TunnelStatus(
            running=False, public_url=None,
            error=None, started_at=None,
            binary_found=_find_cloudflared() is not None,
        )

    def _read_output(self) -> None:
        """Drain cloudflared's stdout looking for the URL banner.
        Lines also go to app.log so dev can see auth / connection
        problems without having to inspect cloudflared directly."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                except Exception:  # noqa: BLE001
                    line = repr(raw)
                if not line:
                    continue
                _LOG.info("cloudflared: %s", line)
                m = _URL_RE.search(line)
                if m and self._url is None:
                    with self._lock:
                        self._url = m.group(0)
                    _LOG.info("tunnel URL ready: %s", self._url)
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("cloudflared reader exiting: %s", exc)

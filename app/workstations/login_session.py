"""Patchright-driven login flow + project URL capture.

The customer's "Login & detect project" button posts to start a session
here. Each session is a daemon thread that:

1. Opens a real (headed) Chrome via patchright with the WS's persistent
   profile and navigates to the Flow tools home.
2. Watches ``page.url`` until it matches the Flow project URL pattern —
   meaning the customer logged in and either picked an existing project
   or created a new one.
3. Captures that URL, runs the on_capture callback (which writes to the
   workstations DB row), and closes the window.

State machine (read by the polling Web UI):

  not_started → opening → waiting_for_login → waiting_for_project
                                                   → captured
                                                   → cancelled (operator)
                                                   → error (browser
                                                     crashed, profile
                                                     unwritable, etc.)

Cancel semantics:
* ``cancel()`` flips a stop_event; the thread exits at its next URL poll
  and closes the browser. The window may flash on screen briefly.
* If the customer manually closes the browser window, the next ``page.url``
  call raises and the session moves to ``cancelled``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional


# Flow project URLs always carry a UUID after ``/project/``. We match the
# canonical UUIDv4 shape (8-4-4-4-12 hex) loosely so any future format
# tweak by Google still trips the capture.
PROJECT_URL_RE = re.compile(
    r"https://labs\.google/fx/tools/flow/project/([0-9a-fA-F-]{8,})"
)


class LoginState(str, Enum):
    NOT_STARTED = "not_started"
    OPENING = "opening"
    WAITING_FOR_LOGIN = "waiting_for_login"
    WAITING_FOR_PROJECT = "waiting_for_project"
    CAPTURED = "captured"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class LoginSnapshot:
    """Public read-only view of a session — safe to serialize."""
    state: LoginState
    captured_url: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Type alias — callable that persists the captured URL to the WS DB row.
CaptureCallback = Callable[[str], None]


class LoginSession:
    """One per workstation; recreate when re-logging in (e.g. account swap)."""

    def __init__(
        self,
        *,
        workstation_id: str,
        profile_path: Path,
        entry_url: str,
        on_capture: CaptureCallback,
        poll_interval_sec: float = 0.5,
    ) -> None:
        self.workstation_id = workstation_id
        self.profile_path = Path(profile_path)
        self.entry_url = entry_url
        self.on_capture = on_capture
        self._poll = max(0.05, poll_interval_sec)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._snap = LoginSnapshot(state=LoginState.NOT_STARTED)
        # Make sure the parent logger has a console handler so our INFO
        # lines actually surface — uvicorn doesn't auto-attach handlers
        # to custom ``flow_harvester.*`` loggers.
        from app.utils.logging import _ensure_parent_logger
        _ensure_parent_logger()
        self._log = logging.getLogger("flow_harvester.login")

    # ------------------------------------------------------------------ public

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> LoginSnapshot:
        with self._lock:
            return LoginSnapshot(
                state=self._snap.state,
                captured_url=self._snap.captured_url,
                error=self._snap.error,
                started_at=self._snap.started_at,
                finished_at=self._snap.finished_at,
            )

    def start(self) -> None:
        """Idempotent: a no-op if a thread is already alive."""
        if self.is_running:
            return
        self._stop.clear()
        self._set(state=LoginState.OPENING, started_at=_now_iso(),
                  finished_at=None, captured_url=None, error=None)
        self._thread = threading.Thread(
            target=self._run,
            name=f"flow-login-{self.workstation_id}",
            daemon=True,
        )
        self._thread.start()

    def cancel(self, *, timeout: float = 10.0) -> bool:
        """Signal stop, wait up to ``timeout`` for the thread to exit."""
        if not self.is_running:
            return True
        self._stop.set()
        assert self._thread is not None
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    # ---------------------------------------------------------------- internals

    def _set(self, **fields) -> None:
        with self._lock:
            for k, v in fields.items():
                setattr(self._snap, k, v)

    def _run(self) -> None:
        """Browser-driving thread body. Catches everything to keep the
        FastAPI server alive even if patchright misbehaves."""
        try:
            self._drive_browser()
        except Exception as exc:  # noqa: BLE001 — surface any escaped error
            self._log.exception("login session crashed: %s", exc)
            self._set(
                state=LoginState.ERROR,
                error=f"{type(exc).__name__}: {exc}",
                finished_at=_now_iso(),
            )

    def _drive_browser(self) -> None:
        from patchright.sync_api import sync_playwright

        self.profile_path.mkdir(parents=True, exist_ok=True)
        # Defensive: an earlier patchright session that died (Ctrl+C,
        # OOM, customer closing the cmd window mid-login) can leave
        # the profile dir's SingletonLock dangling AND a zombie
        # Chrome process still attached. The next launch_persistent
        # _context would then crash with TargetClosedError. Clean it
        # up eagerly — customers can't fix this from the bundled exe.
        from app.workstations.profile_check import clean_profile_lock
        clean_profile_lock(self.profile_path)
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_path),
                channel="chrome",
                headless=False,
                no_viewport=True,
                chromium_sandbox=True,
            )
            try:
                # Belt-and-suspenders URL tracking. patchright's cached
                # ``page.url`` property has been observed to lag behind
                # SPA navigation in Flow, so we also register a
                # ``framenavigated`` event listener and fall back to
                # ``page.evaluate("() => location.href")``. New tabs /
                # popups get the listener via the context's page event.
                event_urls: list[str] = []

                def _on_frame_nav(frame) -> None:
                    if frame.parent_frame is None and frame.url:
                        if frame.url not in event_urls:
                            event_urls.append(frame.url)

                def _on_new_page(new_page) -> None:
                    new_page.on("framenavigated", _on_frame_nav)

                ctx.on("page", _on_new_page)
                for existing in ctx.pages:
                    existing.on("framenavigated", _on_frame_nav)

                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(self.entry_url, wait_until="domcontentloaded")
                try:
                    page.bring_to_front()
                except Exception:  # noqa: BLE001
                    pass

                self._set(state=LoginState.WAITING_FOR_LOGIN)
                self._watch_for_project_url(ctx, event_urls)
            finally:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass

    def _watch_for_project_url(self, ctx, event_urls: list[str]) -> None:
        """Scan every open page until one URL matches the project pattern."""
        while not self._stop.is_set():
            try:
                pages = list(ctx.pages)
            except Exception:
                self._set(state=LoginState.CANCELLED, finished_at=_now_iso())
                return
            if not pages:
                self._set(state=LoginState.CANCELLED, finished_at=_now_iso())
                return

            collected: list[str] = []
            for p in pages:
                try:
                    collected.append(p.url)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    live = p.evaluate("() => location.href")
                    if isinstance(live, str):
                        collected.append(live)
                except Exception:  # noqa: BLE001
                    pass
            collected.extend(event_urls)

            for url in collected:
                match = PROJECT_URL_RE.match(url) if url else None
                if match:
                    captured = match.group(0)
                    self._log.info("login %s captured project url: %s",
                                   self.workstation_id, captured)
                    try:
                        self.on_capture(captured)
                    except Exception as exc:  # noqa: BLE001
                        self._set(
                            state=LoginState.ERROR,
                            error=f"capture callback failed: {exc}",
                            finished_at=_now_iso(),
                        )
                        return
                    self._set(
                        state=LoginState.CAPTURED,
                        captured_url=captured,
                        finished_at=_now_iso(),
                    )
                    return

            if (self._snap.state == LoginState.WAITING_FOR_LOGIN
                    and any("/fx/tools/flow" in u for u in collected if u)):
                self._set(state=LoginState.WAITING_FOR_PROJECT)

            time.sleep(self._poll)

        # stop event tripped
        self._set(state=LoginState.CANCELLED, finished_at=_now_iso())


class LoginSessionRegistry:
    """Process-wide map of workstation_id -> LoginSession.

    Sessions are kept after they finish so the UI can read the captured
    URL / error one final time before they're replaced by a new ``start``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, LoginSession] = {}

    def get(self, workstation_id: str) -> Optional[LoginSession]:
        with self._lock:
            return self._sessions.get(workstation_id)

    def put(self, session: LoginSession) -> None:
        with self._lock:
            self._sessions[session.workstation_id] = session

    def remove(self, workstation_id: str) -> Optional[LoginSession]:
        with self._lock:
            return self._sessions.pop(workstation_id, None)

    def cancel_all(self, *, timeout: float = 10.0) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            s.cancel(timeout=timeout)

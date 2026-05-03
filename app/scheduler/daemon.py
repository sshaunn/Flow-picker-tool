"""Background scheduler daemon.

Wraps ``run_multi_workstation`` in a long-lived thread so the FastAPI
process can spawn one scheduler in the background and keep serving Web
requests. Customer flow:

  1. Customer adds a task in the Web form (``app.tasks.repository``).
  2. Daemon thread picks it up next idle-poll cycle and runs it.
  3. Closing the browser tab does not interrupt anything — the daemon
     keeps the loop running until the queue drains, then idles.
  4. ``stop()`` flips a stop_event; the daemon finishes its current
     scheduling round and exits cleanly. In-flight workers (one per WS)
     finish via the existing ``ThreadPoolExecutor`` drain.

What this module is NOT:
* Not a process supervisor — if the FastAPI server crashes, so does the
  daemon. ``start.bat`` (run-on-login shortcut) handles "always restart".
* Not a per-task thread pool — the inner ``run_multi_workstation``
  already has one keyed by workstation id; the daemon adds only the
  outer "keep the loop going past empty-queue" wrapping.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from app.config.loader import AppConfig, WorkstationConfig
from app.runner.multi import MultiRunSummary, run_multi_workstation
from app.worker.flow_mock import MockRoundPlan
from app.worker.flow_port import PageState


@dataclass
class DaemonStatus:
    """Snapshot of the daemon's recent activity. Returned by ``status()`` for
    the Web UI to render a "scheduler healthy" indicator."""
    running: bool = False
    started_at: Optional[str] = None       # ISO UTC, set when start() returned
    stopped_at: Optional[str] = None       # ISO UTC, set after thread exits
    last_round_at: Optional[str] = None    # ISO UTC of most recent run completion
    rounds_completed: int = 0              # outer loop iterations (1 per run_multi)
    cumulative: MultiRunSummary = field(default_factory=MultiRunSummary)
    last_error: Optional[str] = None       # last unexpected exception text


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SchedulerDaemon:
    """Run the scheduler loop in a daemon thread.

    Lifecycle:

        d = SchedulerDaemon(db_path, config, workstations)
        d.start()              # non-blocking; returns immediately
        ...
        d.stop(timeout=30)     # signals + joins; safe to call when not running
    """

    def __init__(
        self,
        *,
        db_path: Path | str,
        config: AppConfig,
        workstations: Iterable[WorkstationConfig] | None = None,
        idle_poll_sec: float = 5.0,
        max_rounds_per_run: int = 0,
        use_mock: bool = False,
        mock_round_plans_per_ws: Optional[dict[str, list[MockRoundPlan]]] = None,
        mock_initial_state: PageState = PageState.READY,
        logger: logging.Logger | None = None,
    ):
        self._db_path = Path(db_path)
        self._config = config
        # Production: leave None so each pass re-queries the DB and picks
        # up workstations added through the Web UI after the daemon was
        # constructed. Tests pass an explicit list to lock the set.
        self._workstations_override: Optional[list[WorkstationConfig]] = (
            list(workstations) if workstations is not None else None
        )
        self._idle_poll_sec = max(0.1, idle_poll_sec)
        self._max_rounds_per_run = max_rounds_per_run
        self._use_mock = use_mock
        self._mock_plans = mock_round_plans_per_ws
        self._mock_initial_state = mock_initial_state
        self._log = logger or logging.getLogger("flow_harvester.daemon")

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._status_lock = threading.Lock()
        self._status = DaemonStatus()

    # ------------------------------------------------------------------ public

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> DaemonStatus:
        """Snapshot of daemon state — safe to call from any thread."""
        with self._status_lock:
            # Return a shallow copy so caller can't mutate our internal state.
            return DaemonStatus(
                running=self.is_running,
                started_at=self._status.started_at,
                stopped_at=self._status.stopped_at,
                last_round_at=self._status.last_round_at,
                rounds_completed=self._status.rounds_completed,
                cumulative=MultiRunSummary(
                    executed=self._status.cumulative.executed,
                    success=self._status.cumulative.success,
                    failed=self._status.cumulative.failed,
                    download_failed=self._status.cumulative.download_failed,
                    retry_waiting=self._status.cumulative.retry_waiting,
                    manual_review=self._status.cumulative.manual_review,
                ),
                last_error=self._status.last_error,
            )

    def start(self) -> None:
        """Spawn the daemon thread. Idempotent: a no-op if already running."""
        if self.is_running:
            self._log.info("daemon already running, start() is a no-op")
            return
        self._stop_event.clear()
        with self._status_lock:
            self._status = DaemonStatus(
                running=True,
                started_at=_utc_now_iso(),
            )
        self._thread = threading.Thread(
            target=self._run, name="flow-harvester-daemon", daemon=True
        )
        self._thread.start()
        self._log.info("daemon started (idle_poll=%.1fs)", self._idle_poll_sec)

    def stop(self, timeout: float = 30.0) -> bool:
        """Signal the daemon to stop after the current run; join up to ``timeout``.

        Returns True if the thread terminated within ``timeout``; False if it's
        still alive (likely stuck inside a long-running browser action). Safe
        to call when not running.
        """
        if not self.is_running:
            return True
        self._log.info("daemon stop requested")
        self._stop_event.set()
        assert self._thread is not None
        self._thread.join(timeout=timeout)
        joined = not self._thread.is_alive()
        if joined:
            self._log.info("daemon stopped cleanly")
        else:
            self._log.warning(
                "daemon thread did not exit within %.1fs — still alive", timeout
            )
        return joined

    # ----------------------------------------------------------------- internal

    def _run(self) -> None:
        """Daemon thread body. Runs ``run_multi_workstation`` repeatedly,
        idling for ``idle_poll_sec`` between empty-queue exits."""
        try:
            while not self._stop_event.is_set():
                summary = self._run_one_pass()
                self._merge_summary(summary)
                if self._stop_event.is_set():
                    break
                # Empty queue or rounds cap hit — idle until either a stop
                # signal or a poll deadline.
                self._stop_event.wait(self._idle_poll_sec)
        except Exception as exc:  # noqa: BLE001 — surface any escaped error
            self._log.exception("daemon crashed: %s", exc)
            with self._status_lock:
                self._status.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._status_lock:
                self._status.stopped_at = _utc_now_iso()
                self._status.running = False
            self._log.info("daemon thread exiting")

    def _resolve_workstations(self) -> list[WorkstationConfig]:
        if self._workstations_override is not None:
            return self._workstations_override
        from app.db.connection import connect
        from app.workstations.repository import list_workstations
        conn = connect(self._db_path, check_same_thread=False)
        try:
            return list_workstations(conn)
        finally:
            conn.close()

    def _run_one_pass(self) -> MultiRunSummary:
        ws_list = self._resolve_workstations()
        if not ws_list:
            return MultiRunSummary()
        # Resolve the day / night profile for this pass. Read fresh each
        # time so a customer toggling the top-nav switch takes effect on
        # the next idle-poll cycle.
        from app.db.connection import connect as _connect
        from app.state import get_operation_mode
        conn = _connect(self._db_path, check_same_thread=False)
        try:
            mode = get_operation_mode(conn)
        finally:
            conn.close()
        active_profile = getattr(
            self._config.operation_modes, mode.value,
        )
        try:
            return run_multi_workstation(
                db_path=self._db_path,
                config=self._config,
                workstations=ws_list,
                max_rounds=self._max_rounds_per_run,
                use_mock=self._use_mock,
                mock_round_plans_per_ws=self._mock_plans,
                mock_initial_state=self._mock_initial_state,
                mode_profile=active_profile,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.exception("scheduler pass raised: %s", exc)
            with self._status_lock:
                self._status.last_error = f"{type(exc).__name__}: {exc}"
            return MultiRunSummary()

    def _merge_summary(self, summary: MultiRunSummary) -> None:
        with self._status_lock:
            self._status.rounds_completed += 1
            self._status.last_round_at = _utc_now_iso()
            cum = self._status.cumulative
            cum.executed += summary.executed
            cum.success += summary.success
            cum.failed += summary.failed
            cum.download_failed += summary.download_failed
            cum.retry_waiting += summary.retry_waiting
            cum.manual_review += summary.manual_review

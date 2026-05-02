"""Scheduler daemon JSON API.

The daemon auto-starts via the FastAPI lifespan hook so the customer
doesn't need to click anything to begin generating. These routes exist
mostly for emergency stop / status display in the dashboard.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.scheduler.daemon import SchedulerDaemon
from app.web.dependencies import get_daemon


router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


class CumulativeOut(BaseModel):
    executed: int
    success: int
    failed: int
    download_failed: int
    retry_waiting: int
    manual_review: int


class DaemonStatusOut(BaseModel):
    running: bool
    started_at: Optional[str]
    stopped_at: Optional[str]
    last_round_at: Optional[str]
    rounds_completed: int
    cumulative: CumulativeOut
    last_error: Optional[str]


def _to_out(daemon: SchedulerDaemon) -> DaemonStatusOut:
    snap = daemon.status()
    return DaemonStatusOut(
        running=snap.running,
        started_at=snap.started_at,
        stopped_at=snap.stopped_at,
        last_round_at=snap.last_round_at,
        rounds_completed=snap.rounds_completed,
        cumulative=CumulativeOut(
            executed=snap.cumulative.executed,
            success=snap.cumulative.success,
            failed=snap.cumulative.failed,
            download_failed=snap.cumulative.download_failed,
            retry_waiting=snap.cumulative.retry_waiting,
            manual_review=snap.cumulative.manual_review,
        ),
        last_error=snap.last_error,
    )


@router.get("/status", response_model=DaemonStatusOut)
def status_route(daemon: SchedulerDaemon = Depends(get_daemon)) -> DaemonStatusOut:
    return _to_out(daemon)


@router.post("/start", response_model=DaemonStatusOut)
def start_route(daemon: SchedulerDaemon = Depends(get_daemon)) -> DaemonStatusOut:
    """Idempotent — returns immediately if already running."""
    daemon.start()
    return _to_out(daemon)


@router.post("/stop", response_model=DaemonStatusOut)
def stop_route(daemon: SchedulerDaemon = Depends(get_daemon)) -> DaemonStatusOut:
    """Signal the daemon to stop after the current round, wait briefly."""
    daemon.stop(timeout=10.0)
    return _to_out(daemon)

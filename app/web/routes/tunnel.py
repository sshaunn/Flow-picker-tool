"""Tunnel control endpoints — start / stop / status the
``cloudflared`` quick-tunnel that gives dev a public URL into the
customer's running dashboard for live diagnostics.

The actual subprocess management lives in ``app.tunnel``; this module
only brokers between HTTP and the singleton ``TunnelManager`` stored
on ``app.state``.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.tunnel import TunnelManager


router = APIRouter(prefix="/api/tunnel", tags=["tunnel"])


class TunnelStatusOut(BaseModel):
    running: bool
    public_url: Optional[str]
    error: Optional[str]
    started_at: Optional[str]
    binary_found: bool


def _manager(request: Request) -> TunnelManager:
    return request.app.state.tunnel_manager


def _to_out(s) -> TunnelStatusOut:
    return TunnelStatusOut(
        running=s.running,
        public_url=s.public_url,
        error=s.error,
        started_at=s.started_at,
        binary_found=s.binary_found,
    )


@router.get("/status", response_model=TunnelStatusOut)
def status_route(request: Request) -> TunnelStatusOut:
    return _to_out(_manager(request).status())


@router.post("/start", response_model=TunnelStatusOut)
def start_route(request: Request) -> TunnelStatusOut:
    return _to_out(_manager(request).start())


@router.post("/stop", response_model=TunnelStatusOut)
def stop_route(request: Request) -> TunnelStatusOut:
    return _to_out(_manager(request).stop())

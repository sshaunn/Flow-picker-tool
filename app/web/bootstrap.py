"""Reload-mode entrypoint for ``uvicorn app.web.bootstrap:app --reload``.

The CLI's ``serve --reload`` path needs an import string instead of an app
instance. This module reads the same settings via env vars (set by the
CLI) and calls ``create_app``. Customer-facing ``start.bat`` does NOT use
``--reload`` — this exists only for the dev workflow.
"""

from __future__ import annotations

import os

from app.config.loader import load_settings
from app.web.server import create_app


_settings_path = os.environ.get("FLOW_HARVESTER_SETTINGS", "config/settings.yaml")
_auto_start = os.environ.get("FLOW_HARVESTER_AUTO_START", "1") == "1"
_idle_poll = float(os.environ.get("FLOW_HARVESTER_IDLE_POLL_SEC", "5.0"))

app = create_app(
    config=load_settings(_settings_path),
    auto_start_daemon=_auto_start,
    idle_poll_sec=_idle_poll,
)

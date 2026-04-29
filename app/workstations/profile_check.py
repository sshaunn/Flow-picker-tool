"""Workstation profile readiness check (T06).

Before a Worker drives a profile, we want to fail loud if the profile
directory is missing or read-only — otherwise Playwright will error mid-run
and the workstation will look "intermittently broken".

This module is intentionally side-effect-free: it does not flip workstation
status in the DB. Callers (the Runner) decide what to do with the result.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProfileCheckResult:
    ok: bool
    reason: str | None
    path: Path

    @property
    def manual_check_required(self) -> bool:
        return not self.ok


def check_profile(profile_path: Path | str) -> ProfileCheckResult:
    path = Path(profile_path)
    if not path.exists():
        return ProfileCheckResult(False, f"profile path does not exist: {path}", path)
    if not path.is_dir():
        return ProfileCheckResult(False, f"profile path is not a directory: {path}", path)
    if not os.access(path, os.W_OK):
        return ProfileCheckResult(False, f"profile path is not writable: {path}", path)
    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".flow_probe_", delete=True):
            pass
    except OSError as exc:
        return ProfileCheckResult(False, f"profile path probe write failed: {exc}", path)
    return ProfileCheckResult(True, None, path)


def ensure_profile_dir(profile_path: Path | str) -> ProfileCheckResult:
    """Create the profile directory if missing, then run check_profile.

    This is *only* used by ``check`` style commands (operator helpers); the
    Runner does not auto-create profile directories — the operator is
    expected to log into Flow inside the profile manually first.
    """
    path = Path(profile_path)
    path.mkdir(parents=True, exist_ok=True)
    return check_profile(path)

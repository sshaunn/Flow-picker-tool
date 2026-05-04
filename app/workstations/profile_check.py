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


def clean_profile_lock(profile_path: Path | str) -> None:
    """Kill orphan Chrome procs pinned to ``profile_path`` and remove
    Chrome's per-profile Singleton* files.

    Why: Chrome guards each ``--user-data-dir`` with a SingletonLock
    symlink that points at ``<host>-<pid>`` of the live Chrome process.
    On a crash / SIGKILL / customer-closed-cmd-window mid-session,
    that symlink survives. The next launch_persistent_context sees it,
    decides a Chrome is already running, forwards its args via Mojo,
    and the new bootstrap process exits with code 0 — which patchright
    reports as ``TargetClosedError: Target page, context or browser
    has been closed``. Customer can't fix this without shell access on
    the bundled exe; do it eagerly here whenever a fresh patchright
    session is about to launch.

    Safe to call before every ``launch_persistent_context``: at the
    moment of a new session start, nothing legitimate should be
    holding the profile (login + worker on the same WS are mutually
    exclusive, and worker close() runs before the next worker open()).
    Either it's a no-op (clean state) or it kills an orphan.
    """
    target = str(Path(profile_path)).rstrip(os.sep).rstrip("/")
    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None  # type: ignore[assignment]

    if psutil is not None:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = proc.info.get("cmdline") or []
                if any(target in str(arg) for arg in cmd):
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                continue
    else:
        # Cross-platform shell-out fallback (Win bundle ships its own
        # Python so psutil is usually present, but defend anyway).
        import subprocess as _sub
        if os.name == "nt":
            try:
                out = _sub.run(
                    ["wmic", "process", "where",
                     "name='chrome.exe'", "get",
                     "ProcessId,CommandLine", "/format:list"],
                    capture_output=True, text=True, timeout=5,
                ).stdout or ""
                cur_match = False
                for line in out.splitlines():
                    if line.startswith("CommandLine="):
                        cur_match = target in line
                    elif line.startswith("ProcessId=") and cur_match:
                        pid = line.split("=", 1)[1].strip()
                        if pid:
                            _sub.run(["taskkill", "/F", "/PID", pid],
                                     timeout=2, check=False)
                        cur_match = False
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                out = _sub.run(
                    ["ps", "axo", "pid=,args="],
                    capture_output=True, text=True, timeout=5,
                ).stdout or ""
                for line in out.splitlines():
                    if target in line:
                        pid = line.strip().split(None, 1)[0]
                        try:
                            _sub.run(["kill", "-9", pid], timeout=2,
                                     check=False)
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001
                pass

    # Drop the lock files. unlink rather than rmtree — keep cookies /
    # extensions / cache, only kill the lock state.
    profile = Path(profile_path)
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (profile / name).unlink()
        except (FileNotFoundError, OSError):
            pass


def ensure_profile_dir(profile_path: Path | str) -> ProfileCheckResult:
    """Create the profile directory if missing, then run check_profile.

    This is *only* used by ``check`` style commands (operator helpers); the
    Runner does not auto-create profile directories — the operator is
    expected to log into Flow inside the profile manually first.
    """
    path = Path(profile_path)
    path.mkdir(parents=True, exist_ok=True)
    return check_profile(path)

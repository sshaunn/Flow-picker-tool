"""Cross-platform application data paths.

Resolves where Flow Harvester stores its runtime artifacts (DB, profiles,
output videos, logs) on macOS / Windows / Linux. Used as defaults when
``settings.yaml`` omits the explicit ``output_root`` / ``db_path`` /
``log_root`` keys, so the customer-side install can run without editing
any YAML at all. Existing dev configs that pin relative paths still work
unchanged because explicit yaml values override these defaults.

Layout:

  macOS:    ~/Library/Application Support/FlowHarvester/
  Windows:  %LOCALAPPDATA%\\FlowHarvester\\
  Linux:    $XDG_DATA_HOME/FlowHarvester  (fallback: ~/.local/share/FlowHarvester)

Subdirs / files (under the resolved app_data_dir):
  profiles/                 per-workstation Chrome user-data dirs
  logs/                     scheduler.log + worker_<id>.log + errors.log
  config/                   user-editable settings
  flow_harvester.sqlite     main DB

Output videos (``output_root``) are kept SEPARATE from app_data_dir on
Windows: customers expect to find their generated mp4s under
``Documents\\FlowHarvester\\output\\`` rather than buried inside AppData
where Explorer hides them by default. macOS / Linux co-locate everything
under app_data_dir for simplicity.

Override for tests / advanced users: set ``FLOW_HARVESTER_DATA_DIR`` in the
environment to relocate the entire app_data_dir; ``output_root()`` still
goes to its platform-default location unless it too is co-located via the
override.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "FlowHarvester"

_ENV_DATA_DIR = "FLOW_HARVESTER_DATA_DIR"
_ENV_OUTPUT_DIR = "FLOW_HARVESTER_OUTPUT_DIR"


def app_data_dir() -> Path:
    """Top-level directory under which all per-user state lives."""
    override = os.environ.get(_ENV_DATA_DIR)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def output_root() -> Path:
    """Where generated videos / daily reports land.

    On Windows we deliberately point at ``%USERPROFILE%\\Documents\\FlowHarvester\\output``
    so the customer can drag-and-drop finished mp4s straight from Explorer
    without opening hidden AppData. macOS / Linux co-locate under
    ``app_data_dir()`` since neither hides ``Library``/``.local``.
    """
    override = os.environ.get(_ENV_OUTPUT_DIR)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        userprofile = os.environ.get("USERPROFILE")
        base = Path(userprofile) if userprofile else Path.home()
        return base / "Documents" / APP_NAME / "output"
    return app_data_dir() / "output"


def profiles_dir() -> Path:
    return app_data_dir() / "profiles"


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def db_path() -> Path:
    return app_data_dir() / "flow_harvester.sqlite"


def config_dir() -> Path:
    return app_data_dir() / "config"


def assets_dir() -> Path:
    """Where uploaded source images for tasks live.

    The Web UI form copies the customer's uploaded image into
    ``assets_dir() / <task_id> / <order>_<original_name>`` so the worker's
    later upload step has a stable path even after the browser POST is
    forgotten.
    """
    return app_data_dir() / "assets"


def workstation_profile_path(workstation_id: str) -> Path:
    """Resolve the on-disk Chrome profile dir for a given workstation id.

    Used when creating a new workstation record so the profile path is
    stored as an absolute platform-specific location instead of the
    legacy ``./profiles/workstation_X`` relative form.
    """
    if not workstation_id or "/" in workstation_id or "\\" in workstation_id:
        raise ValueError(f"invalid workstation id: {workstation_id!r}")
    return profiles_dir() / workstation_id


def ensure_app_dirs() -> None:
    """Create app-data subdirs that the app expects to exist (idempotent).

    Called once at process startup (CLI / Web server entrypoint) so first
    runs on a fresh customer machine don't fail with ``ENOENT`` halfway
    through opening a log file.
    """
    for d in (app_data_dir(), profiles_dir(), logs_dir(), config_dir(),
              assets_dir(), output_root()):
        d.mkdir(parents=True, exist_ok=True)

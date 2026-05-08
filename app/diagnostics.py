"""Build a single zipped bundle the operator can attach to a support
email when reporting a bug.

Customer-side debugging without this means asking the operator to
hand-collect the logs dir, the DB, and screenshots — at which point
something always gets missed. One button → one zip → one attachment.

The bundle deliberately includes:

  * ``app.log`` + ``scheduler.log`` + per-WS worker logs (truncated
    to recent N MB so the zip stays mailable)
  * ``errors.log`` in full (small by definition)
  * ``crash.log`` in full (also small, only present after a crash)
  * ``login_<ws>_<ts>.png`` and ``login_<ws>_<ts>.txt`` from the
    forensic deep dump in login_session
  * A read-only SQLite backup of ``flow_harvester.sqlite`` so dev can
    open it locally and see workstation / task / error_logs state
  * A redacted ``settings.yaml`` (license-related fields stripped)
  * ``meta.txt`` with OS version, Python version, app version,
    timestamp

It deliberately EXCLUDES:

  * ``license.key`` (HMAC secret material, customer-specific)
  * ``profiles/`` (Chrome profiles — too large, contain cookies)
  * ``output/`` (mp4 files, too large)
  * ``assets/`` (uploaded images, may be customer-confidential)
"""

from __future__ import annotations

import logging
import platform
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app import paths as app_paths


_LOG = logging.getLogger("flow_harvester.diagnostics")

# Per-log-file cap inside the zip. Most app.log files stay under this;
# heavy customer machines after a long run can balloon to 50+ MB which
# is too large to email and too noisy to read end-to-end.
_PER_LOG_TAIL_MB = 5

# Screenshot cap by count, taking the newest first. Forensic deep dumps
# accumulate one .png per stalled login attempt — over a long debug
# session this can reach hundreds, but only the newest are useful.
_MAX_SCREENSHOTS = 30


def build_diagnostic_bundle(out_dir: Path | None = None) -> Path:
    """Build the bundle and return its path. Idempotent: each call
    produces a fresh timestamped zip; old ones are not auto-deleted
    (operator can clean up via Explorer).
    """
    out_dir = Path(out_dir) if out_dir else app_paths.logs_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle_path = out_dir / f"diagnostic_{ts}.zip"

    log_dir = app_paths.logs_dir()
    db_path = app_paths.db_path()

    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        _add_meta(zf, ts)
        _add_logs(zf, log_dir)
        _add_screenshots(zf, log_dir)
        _add_login_dumps(zf, log_dir)
        _add_db_snapshot(zf, db_path)
        _add_settings_redacted(zf)

    _LOG.info("diagnostic bundle built: %s (%d bytes)",
              bundle_path, bundle_path.stat().st_size)
    return bundle_path


def _add_meta(zf: zipfile.ZipFile, ts: str) -> None:
    try:
        from app import __version__ as app_version  # type: ignore
    except Exception:  # noqa: BLE001
        app_version = "unknown"
    lines = [
        f"timestamp={ts}",
        f"platform={platform.platform()}",
        f"system={platform.system()} {platform.release()}",
        f"machine={platform.machine()}",
        f"python={sys.version.split()[0]}",
        f"app_version={app_version}",
        f"frozen={getattr(sys, 'frozen', False)}",
        f"app_data_dir={app_paths.app_data_dir()}",
        f"output_root={app_paths.output_root()}",
    ]
    zf.writestr("meta.txt", "\n".join(lines) + "\n")


def _add_logs(zf: zipfile.ZipFile, log_dir: Path) -> None:
    if not log_dir.exists():
        return
    cap = _PER_LOG_TAIL_MB * 1024 * 1024
    for path in sorted(log_dir.glob("*.log")):
        try:
            data = path.read_bytes()
        except OSError as exc:
            zf.writestr(f"logs/{path.name}.read-error",
                        f"failed to read: {exc}")
            continue
        if len(data) > cap:
            data = b"<truncated head>\n" + data[-cap:]
        zf.writestr(f"logs/{path.name}", data)
    # Rotated logs (e.g. app.log.1) — skip if huge, just include the head/tail
    for path in sorted(log_dir.glob("*.log.*")):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > cap * 2:
            continue  # too big, skip rotated logs entirely
        try:
            zf.writestr(f"logs/{path.name}", path.read_bytes())
        except OSError as exc:
            zf.writestr(f"logs/{path.name}.read-error",
                        f"failed to read: {exc}")


def _add_screenshots(zf: zipfile.ZipFile, log_dir: Path) -> None:
    if not log_dir.exists():
        return
    images = sorted(
        log_dir.glob("*.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:_MAX_SCREENSHOTS]
    for img in images:
        try:
            zf.writestr(f"screenshots/{img.name}", img.read_bytes())
        except OSError as exc:
            zf.writestr(f"screenshots/{img.name}.read-error",
                        f"failed to read: {exc}")


def _add_login_dumps(zf: zipfile.ZipFile, log_dir: Path) -> None:
    """Forensic deep-dump body text from login_session — sibling to
    the screenshots, written when login is stuck > 30s."""
    if not log_dir.exists():
        return
    for txt in sorted(log_dir.glob("login_*.txt"))[-_MAX_SCREENSHOTS:]:
        try:
            zf.writestr(f"screenshots/{txt.name}", txt.read_bytes())
        except OSError as exc:
            zf.writestr(f"screenshots/{txt.name}.read-error",
                        f"failed to read: {exc}")


def _add_db_snapshot(zf: zipfile.ZipFile, db_path: Path) -> None:
    """Use SQLite's online backup API so the snapshot is consistent
    even while the app is actively writing. Naive ``shutil.copy``
    would race against a write transaction and produce a partially
    written DB."""
    if not db_path.exists():
        return
    snapshot_path = db_path.parent / f".diagnostic_snapshot_{datetime.now(timezone.utc).strftime('%H%M%S')}.sqlite"
    try:
        src = sqlite3.connect(str(db_path))
        try:
            dest = sqlite3.connect(str(snapshot_path))
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()
        zf.writestr("flow_harvester.sqlite", snapshot_path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        zf.writestr("flow_harvester.sqlite.read-error",
                    f"backup failed: {exc}")
    finally:
        try:
            snapshot_path.unlink(missing_ok=True)
        except OSError:
            pass


def _add_settings_redacted(zf: zipfile.ZipFile) -> None:
    """Best-effort: dev's settings.yaml (in repo root or bundle's
    config/), not customer-edited. Redact any fields whose name
    looks secret-shaped just in case (license / hmac / secret / key /
    token), even though current settings.yaml has none."""
    candidates = [
        Path("config/settings.yaml"),
        app_paths.config_dir() / "settings.yaml",
    ]
    if getattr(sys, "frozen", False):
        candidates.append(Path(getattr(sys, "_MEIPASS", "")) / "config/settings.yaml")
    for p in candidates:
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        redacted = _redact_secrets(text)
        zf.writestr("settings.yaml", redacted)
        return


def _redact_secrets(text: str) -> str:
    """Cheap line-level redaction: any line whose key matches a
    secret-shaped pattern gets its value replaced. Misses YAML
    multiline / nested structures, but settings.yaml is flat."""
    secret_words = ("license", "secret", "token", "password", "hmac")
    out_lines = []
    for line in text.splitlines():
        lower = line.lower()
        if any(w in lower.split(":", 1)[0] for w in secret_words):
            key = line.split(":", 1)[0]
            out_lines.append(f"{key}: <redacted>")
        else:
            out_lines.append(line)
    return "\n".join(out_lines) + "\n"

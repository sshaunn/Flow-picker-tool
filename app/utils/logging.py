"""Structured logging helpers (T05).

A thin wrapper around stdlib ``logging`` that:

* writes ``scheduler.log``, ``worker_{ws_id}.log`` and a shared ``errors.log``
  under ``log_root``;
* keeps console output too, so ``run-once`` is debuggable without ``tail``;
* emits ``task_id`` and ``workstation_id`` as part of the log message
  (no JSON formatter dependency in MVP).
"""

from __future__ import annotations

import logging
import sys
from logging import Logger
from pathlib import Path

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _file_handler(path: Path, level: int = logging.INFO) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(path, encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(logging.Formatter(_FORMAT))
    return h


def _console_handler(level: int = logging.INFO) -> logging.Handler:
    h = logging.StreamHandler(sys.stderr)
    h.setLevel(level)
    h.setFormatter(logging.Formatter(_FORMAT))
    return h


def _ensure_parent_logger() -> None:
    """Make sure ``flow_harvester`` (parent) has a console handler so that
    descendants without explicit handlers (e.g. ``flow_harvester.playwright``)
    still emit INFO-level records to stderr instead of being swallowed by
    Python's lastResort filter (which only forwards WARNING+).

    Idempotent: re-running adds no extra handlers.
    """
    parent = logging.getLogger("flow_harvester")
    if any(getattr(h, "_flow_harvester_parent", False) for h in parent.handlers):
        return
    parent.setLevel(logging.INFO)
    h = _console_handler()
    h._flow_harvester_parent = True  # type: ignore[attr-defined]
    parent.addHandler(h)


def get_scheduler_logger(log_root: Path | str) -> Logger:
    _ensure_parent_logger()
    log = logging.getLogger("flow_harvester.scheduler")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.addHandler(_file_handler(Path(log_root) / "scheduler.log"))
    log.addHandler(_file_handler(Path(log_root) / "errors.log", logging.WARNING))
    log.addHandler(_console_handler())
    log.propagate = False
    return log


def get_worker_logger(log_root: Path | str, workstation_id: str) -> Logger:
    _ensure_parent_logger()
    log = logging.getLogger(f"flow_harvester.worker.{workstation_id}")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.addHandler(_file_handler(Path(log_root) / f"worker_{workstation_id}.log"))
    log.addHandler(_file_handler(Path(log_root) / "errors.log", logging.WARNING))
    log.addHandler(_console_handler())
    log.propagate = False
    return log


def fmt_task_ctx(task_id: str | None, workstation_id: str | None, **extra) -> str:
    parts = []
    if task_id:
        parts.append(f"task_id={task_id}")
    if workstation_id:
        parts.append(f"workstation_id={workstation_id}")
    for k, v in extra.items():
        if v is not None:
            parts.append(f"{k}={v}")
    return " ".join(parts)

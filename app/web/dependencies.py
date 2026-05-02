"""Shared FastAPI dependencies.

DB connection is per-request: opened in ``get_db_conn`` and closed when
the request finishes. The scheduler daemon is a single instance pinned to
``app.state.daemon`` for the lifetime of the server process.
"""

from __future__ import annotations

import sqlite3
from typing import Generator

from fastapi import Request

from app.config.loader import AppConfig
from app.db.connection import connect
from app.scheduler.daemon import SchedulerDaemon


def get_db_conn(request: Request) -> Generator[sqlite3.Connection, None, None]:
    """Open a fresh sqlite3 connection per HTTP request and close when done.

    SQLite connections are not thread-safe across the request boundary so
    we don't reuse a global pool. The PRAGMA flips here mirror what
    ``app.db.connection.connect`` already enables (foreign keys + row
    factory) — duplicate to make the dependency self-contained.
    """
    cfg: AppConfig = request.app.state.config
    conn = connect(cfg.db_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def get_daemon(request: Request) -> SchedulerDaemon:
    return request.app.state.daemon


def get_config(request: Request) -> AppConfig:
    return request.app.state.config

"""SQLite connection helpers.

* ``connect``    Returns a sqlite3.Connection with sane defaults (foreign keys
                  enabled, ``Row`` factory, ``PRAGMA journal_mode=WAL``).
* ``transaction`` Context manager that issues ``BEGIN IMMEDIATE`` so concurrent
                  schedulers cannot both grab the same row.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        isolation_level=None,  # autocommit; we manage transactions explicitly
        detect_types=sqlite3.PARSE_DECLTYPES,
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, mode: str = "IMMEDIATE") -> Iterator[sqlite3.Connection]:
    """Wrap a block in a SQLite transaction.

    Defaults to ``BEGIN IMMEDIATE`` so two schedulers cannot both win the same
    workstation/task pair (see workflow-and-scheduling.md).
    """
    conn.execute(f"BEGIN {mode}")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

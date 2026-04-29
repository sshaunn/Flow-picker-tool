"""CSV task importer (T03).

Validates each row, points out the offending line number, and refuses to
silently fall back to defaults for required fields. Per docs/development-plan.md
T03:

* Required columns: task_id, sku_id, creative_id, segment_id, sequence_index,
  source_asset_path, source_asset_type, video_prompt, target_count.
* Optional columns: depends_on_task_id, max_retry_count.
* ``source_asset_path`` must point at an existing file.
* ``target_count`` must be > 0.
* ``source_asset_type`` must be in the enum.
* ``(creative_id, segment_id)`` must not collide with an *active* task in DB.
* ``max_retry_count`` from CSV overrides the default; missing column uses the
  caller-supplied default. ``< 0`` is always rejected at row level.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.db.connection import connect, transaction


REQUIRED_COLUMNS = (
    "task_id",
    "sku_id",
    "creative_id",
    "segment_id",
    "sequence_index",
    "source_asset_path",
    "source_asset_type",
    "video_prompt",
    "target_count",
)
OPTIONAL_COLUMNS = ("depends_on_task_id", "max_retry_count")
ALLOWED_SOURCE_TYPES = {
    "first_frame", "last_frame",
    "previous_segment_frame", "reference", "other",
}
TERMINAL_STATUSES = ("success", "failed", "download_failed")
# Pipe ``|`` joins multiple ordered assets per task in the CSV cell.
# Real file paths almost never contain ``|`` so it's a safe split char.
ASSET_DELIM = "|"


class ImportError(ValueError):
    """Raised when the CSV cannot be imported safely."""


@dataclass
class ImportSummary:
    inserted: int
    skipped: int


def _validate_header(fieldnames: Iterable[str] | None) -> None:
    if not fieldnames:
        raise ImportError("CSV has no header row")
    cols = set(fieldnames)
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise ImportError(f"missing required columns: {', '.join(missing)}")


def _validate_row(row: dict[str, str], line_no: int, asset_root: Path | None) -> dict:
    def _required(field: str) -> str:
        v = (row.get(field) or "").strip()
        if not v:
            raise ImportError(f"line {line_no}: required field '{field}' is empty")
        return v

    task_id = _required("task_id")
    sku_id = _required("sku_id")
    creative_id = _required("creative_id")
    segment_id = _required("segment_id")
    if any(c in segment_id for c in "/\\.."):
        raise ImportError(f"line {line_no}: segment_id contains illegal characters: {segment_id!r}")
    if any(c in creative_id for c in "/\\.."):
        raise ImportError(f"line {line_no}: creative_id contains illegal characters: {creative_id!r}")
    if any(c in sku_id for c in "/\\.."):
        raise ImportError(f"line {line_no}: sku_id contains illegal characters: {sku_id!r}")

    try:
        sequence_index = int(_required("sequence_index"))
    except ValueError as exc:
        raise ImportError(f"line {line_no}: sequence_index must be int") from exc

    raw_paths = _required("source_asset_path")
    raw_types = _required("source_asset_type")
    asset_paths = [p.strip() for p in raw_paths.split(ASSET_DELIM) if p.strip()]
    asset_types = [t.strip() for t in raw_types.split(ASSET_DELIM) if t.strip()]
    if not asset_paths:
        raise ImportError(f"line {line_no}: source_asset_path is empty after split")
    # Allow a single ``source_asset_type`` to broadcast across all paths
    # (common for Ingredients mode where every image is a 'reference').
    if len(asset_types) == 1 and len(asset_paths) > 1:
        asset_types = asset_types * len(asset_paths)
    if len(asset_types) != len(asset_paths):
        raise ImportError(
            f"line {line_no}: source_asset_path has {len(asset_paths)} entries "
            f"but source_asset_type has {len(asset_types)} entries; either "
            f"counts must match or pass a single shared type"
        )
    for idx, (path, atype) in enumerate(zip(asset_paths, asset_types), start=1):
        asset = Path(path)
        if not asset.is_absolute() and asset_root is not None:
            asset_check = asset_root / asset
        else:
            asset_check = asset
        if not asset_check.exists():
            raise ImportError(
                f"line {line_no}: asset #{idx} does not exist: {path}"
            )
        if not asset_check.is_file():
            raise ImportError(
                f"line {line_no}: asset #{idx} is not a file: {path}"
            )
        if atype not in ALLOWED_SOURCE_TYPES:
            raise ImportError(
                f"line {line_no}: asset #{idx} type must be one of "
                f"{sorted(ALLOWED_SOURCE_TYPES)}, got {atype!r}"
            )
    # Mirror the *first* asset onto the legacy ``tasks.source_asset_*``
    # columns so existing queries/reports keep working when they only
    # need a representative asset.
    source_asset_path = asset_paths[0]
    source_asset_type = asset_types[0]

    video_prompt = _required("video_prompt")

    try:
        target_count = int(_required("target_count"))
    except ValueError as exc:
        raise ImportError(f"line {line_no}: target_count must be int") from exc
    if target_count <= 0:
        raise ImportError(f"line {line_no}: target_count must be > 0, got {target_count}")

    depends_on_raw = (row.get("depends_on_task_id") or "").strip()
    depends_on = depends_on_raw or None

    max_retry_raw = (row.get("max_retry_count") or "").strip()
    max_retry: int | None
    if max_retry_raw == "":
        max_retry = None
    else:
        try:
            max_retry = int(max_retry_raw)
        except ValueError as exc:
            raise ImportError(f"line {line_no}: max_retry_count must be int") from exc
        if max_retry < 0:
            raise ImportError(
                f"line {line_no}: max_retry_count must be >= 0, got {max_retry}"
            )

    return {
        "task_id": task_id,
        "sku_id": sku_id,
        "creative_id": creative_id,
        "segment_id": segment_id,
        "sequence_index": sequence_index,
        "source_asset_path": source_asset_path,
        "source_asset_type": source_asset_type,
        "video_prompt": video_prompt,
        "target_count": target_count,
        "depends_on_task_id": depends_on,
        "max_retry_count": max_retry,
        "asset_paths": asset_paths,
        "asset_types": asset_types,
    }


def _has_active_task(conn: sqlite3.Connection, creative_id: str, segment_id: str) -> bool:
    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
    cursor = conn.execute(
        f"""
        SELECT 1 FROM tasks
        WHERE creative_id = ? AND segment_id = ?
          AND status NOT IN ({placeholders})
        LIMIT 1
        """,
        (creative_id, segment_id, *TERMINAL_STATUSES),
    )
    return cursor.fetchone() is not None


def import_tasks(
    csv_path: Path | str,
    db_path: Path | str,
    *,
    default_max_retry: int = 2,
    asset_root: Path | None = None,
) -> ImportSummary:
    csv_path = Path(csv_path)
    if asset_root is None:
        # Resolve relative paths against the current working directory so
        # ``source_asset_path`` columns can be repo-root-relative regardless
        # of where the CSV lives.
        asset_root = Path.cwd()

    rows: list[dict] = []
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        _validate_header(reader.fieldnames)
        for line_no, raw_row in enumerate(reader, start=2):
            rows.append(_validate_row(raw_row, line_no, asset_root))

    seen_task_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for row in rows:
        if row["task_id"] in seen_task_ids:
            raise ImportError(f"duplicate task_id in CSV: {row['task_id']}")
        seen_task_ids.add(row["task_id"])
        pair = (row["creative_id"], row["segment_id"])
        if pair in seen_pairs:
            raise ImportError(
                f"duplicate (creative_id, segment_id) in CSV: {pair[0]} / {pair[1]}"
            )
        seen_pairs.add(pair)

    conn = connect(db_path)
    try:
        with transaction(conn):
            for row in rows:
                if _has_active_task(conn, row["creative_id"], row["segment_id"]):
                    raise ImportError(
                        f"active task already exists for ({row['creative_id']}, {row['segment_id']})"
                    )
                max_retry = (
                    default_max_retry
                    if row["max_retry_count"] is None
                    else row["max_retry_count"]
                )
                try:
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            task_id, sku_id, creative_id, segment_id, sequence_index,
                            source_asset_path, source_asset_type, video_prompt,
                            target_count, depends_on_task_id, max_retry_count, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                        """,
                        (
                            row["task_id"],
                            row["sku_id"],
                            row["creative_id"],
                            row["segment_id"],
                            row["sequence_index"],
                            row["source_asset_path"],
                            row["source_asset_type"],
                            row["video_prompt"],
                            row["target_count"],
                            row["depends_on_task_id"],
                            max_retry,
                        ),
                    )
                    # Persist all assets — single-asset tasks get one row,
                    # frame pairs / reference lists get N. Worker reads
                    # task_assets in asset_order ASC to honor first/last
                    # frame ordering or reference layering.
                    for order_idx, (path, atype) in enumerate(
                        zip(row["asset_paths"], row["asset_types"]), start=1
                    ):
                        conn.execute(
                            """
                            INSERT INTO task_assets
                                (task_id, asset_order, asset_path, asset_type)
                            VALUES (?, ?, ?, ?)
                            """,
                            (row["task_id"], order_idx, path, atype),
                        )
                except sqlite3.IntegrityError as exc:
                    raise ImportError(
                        f"failed to insert task_id={row['task_id']}: {exc}"
                    ) from exc
        return ImportSummary(inserted=len(rows), skipped=0)
    finally:
        conn.close()

"""DB-first CRUD for task records (form-based task creation).

The Web UI's "New task" form posts here instead of writing CSV. The CSV
importer (`app.tasks.importer`) stays for bulk / power-user paths and
ends up at the same ``tasks`` + ``task_assets`` rows.

Form-side input is captured as a ``TaskDraft`` (kept distinct from the
``TaskInput`` runtime shape in ``app.worker.loop`` so the two can evolve
independently). ``create_task`` validates, optionally copies the uploaded
images into the managed assets dir, and writes both tables atomically.

What is NOT here:
* Cancellation / mid-flight task editing — V1 expects pending tasks to
  either run or be deleted before they're picked up.
* Bulk operations — the CSV importer covers that case.
"""

from __future__ import annotations

import re
import secrets
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app import paths
from app.config.loader import FlowModeSpec
from app.db.connection import transaction


ALLOWED_ASSET_TYPES = frozenset({
    "first_frame", "last_frame",
    "previous_segment_frame", "reference", "other",
})

# (creative_id, segment_id) cannot collide with an active task.
_TERMINAL_STATUSES = ("success", "failed", "download_failed")
# Field-level path-traversal guard for sku/creative/segment identifiers.
_BAD_ID_TOKENS = ("/", "\\", "..")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class TaskRepositoryError(ValueError):
    """Validation / conflict error from task CRUD."""


class TaskNotFoundError(LookupError):
    pass


@dataclass
class AssetDraft:
    """One source image attached to a task draft.

    ``path`` must point at an existing file before ``create_task`` runs.
    Set ``copy_into_managed_dir=True`` to have the repository copy the
    source file into ``paths.assets_dir() / task_id / ...`` so the worker
    isn't reading a temp upload that's about to be deleted.
    """
    path: Path
    kind: str = "reference"
    copy_into_managed_dir: bool = True

    def __post_init__(self) -> None:
        if self.kind not in ALLOWED_ASSET_TYPES:
            raise TaskRepositoryError(
                f"asset kind must be one of {sorted(ALLOWED_ASSET_TYPES)}, "
                f"got {self.kind!r}"
            )


@dataclass
class TaskDraft:
    """Form-style input for ``create_task``.

    ``task_id`` is auto-generated (``T_<utc-yyyymmdd-hhmmss>_<6 hex>``) when
    omitted so the UI doesn't have to invent one. Pass an explicit value
    only when restoring from a backup or migrating between systems.

    ``flow_mode`` is an optional per-task override; any field left None
    falls back to the assigned workstation's preset at run time. This
    lets the same workstation drive tasks with different model / duration
    / aspect / output-count combinations without re-configuring the WS.
    """
    sku_id: str
    creative_id: str
    segment_id: str
    video_prompt: str
    target_count: int
    assets: list[AssetDraft] = field(default_factory=list)
    sequence_index: int = 1
    depends_on_task_id: str | None = None
    max_retry_count: int | None = None
    task_id: str | None = None
    flow_mode: FlowModeSpec | None = None


@dataclass
class TaskRecord:
    """Read shape returned by list/get."""
    task_id: str
    sku_id: str
    creative_id: str
    segment_id: str
    sequence_index: int
    video_prompt: str
    target_count: int
    downloaded_count: int
    generation_round_count: int
    status: str
    assigned_workstation_id: str | None
    retry_count: int
    max_retry_count: int
    depends_on_task_id: str | None
    source_asset_path: str
    source_asset_type: str
    error_type: str | None
    error_message: str | None
    created_at: str
    flow_mode: FlowModeSpec | None = None


def generate_task_id() -> str:
    """Generate a sortable, collision-resistant task id.

    Format: ``T_YYYYMMDDTHHMMSS_<6 hex>``. Year-first ordering means
    ``ORDER BY task_id`` matches creation order; the random suffix keeps
    sub-second creates from colliding.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"T_{ts}_{secrets.token_hex(3)}"


def _validate_id_component(name: str, value: str) -> str:
    if not value or not value.strip():
        raise TaskRepositoryError(f"{name} must be non-empty")
    if value != value.strip():
        raise TaskRepositoryError(f"{name} must not have leading/trailing whitespace")
    for token in _BAD_ID_TOKENS:
        if token in value:
            raise TaskRepositoryError(
                f"{name} contains illegal token {token!r}: {value!r}"
            )
    return value


def _sanitize_filename(name: str) -> str:
    """Make a filename safe for the assets dir without changing the extension."""
    name = name.replace(" ", "_")
    safe = _SAFE_FILENAME_RE.sub("_", name).strip("_")
    return safe or "asset"


def _copy_into_assets_dir(src: Path, *, task_id: str, order: int) -> Path:
    """Copy ``src`` into ``assets_dir/task_id/<order>_<sanitized_name>``."""
    dest_dir = paths.assets_dir() / task_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{order:02d}_{_sanitize_filename(src.name)}"
    shutil.copy2(src, dest)
    return dest


def _has_active_task(
    conn: sqlite3.Connection, creative_id: str, segment_id: str
) -> bool:
    placeholders = ",".join("?" for _ in _TERMINAL_STATUSES)
    cursor = conn.execute(
        f"""
        SELECT 1 FROM tasks
         WHERE creative_id = ? AND segment_id = ?
           AND status NOT IN ({placeholders})
         LIMIT 1
        """,
        (creative_id, segment_id, *_TERMINAL_STATUSES),
    )
    return cursor.fetchone() is not None


def create_task(
    conn: sqlite3.Connection,
    draft: TaskDraft,
    *,
    default_max_retry: int = 2,
) -> str:
    """Create a task + its asset rows from a form-style draft.

    Returns the assigned ``task_id`` (auto-generated when ``draft.task_id``
    was omitted). Raises ``TaskRepositoryError`` for validation issues,
    sqlite3 errors propagate as-is.
    """
    sku = _validate_id_component("sku_id", draft.sku_id)
    creative = _validate_id_component("creative_id", draft.creative_id)
    segment = _validate_id_component("segment_id", draft.segment_id)

    prompt = draft.video_prompt.strip()
    if not prompt:
        raise TaskRepositoryError("video_prompt must be non-empty")

    if draft.target_count <= 0:
        raise TaskRepositoryError(
            f"target_count must be > 0, got {draft.target_count}"
        )

    if draft.sequence_index < 0:
        raise TaskRepositoryError(
            f"sequence_index must be >= 0, got {draft.sequence_index}"
        )

    if not draft.assets:
        raise TaskRepositoryError("at least one source asset is required")

    for asset in draft.assets:
        if not asset.path.exists():
            raise TaskRepositoryError(f"asset does not exist: {asset.path}")
        if not asset.path.is_file():
            raise TaskRepositoryError(f"asset is not a file: {asset.path}")

    max_retry = (
        default_max_retry
        if draft.max_retry_count is None
        else draft.max_retry_count
    )
    if max_retry < 0:
        raise TaskRepositoryError(
            f"max_retry_count must be >= 0, got {max_retry}"
        )

    if _has_active_task(conn, creative, segment):
        raise TaskRepositoryError(
            f"active task already exists for ({creative}, {segment})"
        )

    task_id = draft.task_id or generate_task_id()
    if draft.task_id is not None:
        _validate_id_component("task_id", draft.task_id)

    # Resolve final on-disk paths for each asset (copy now if requested).
    final_assets: list[tuple[Path, str]] = []
    for order_idx, asset in enumerate(draft.assets, start=1):
        if asset.copy_into_managed_dir:
            dest = _copy_into_assets_dir(asset.path, task_id=task_id, order=order_idx)
        else:
            dest = asset.path
        final_assets.append((dest, asset.kind))

    primary_path, primary_kind = final_assets[0]

    fm = draft.flow_mode
    fm_tab = fm.tab if fm else None
    fm_subtab = fm.subtab if fm else None
    fm_aspect = fm.aspect if fm else None
    fm_output = fm.output_count if fm else None
    fm_duration = fm.duration_sec if fm else None
    fm_model = fm.model if fm else None

    with transaction(conn):
        try:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, sku_id, creative_id, segment_id, sequence_index,
                    source_asset_path, source_asset_type, video_prompt,
                    target_count, depends_on_task_id, max_retry_count, status,
                    flow_mode_tab, flow_mode_subtab, flow_mode_aspect,
                    flow_mode_output_count, flow_mode_duration_sec, flow_mode_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending',
                          ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, sku, creative, segment, draft.sequence_index,
                    str(primary_path), primary_kind, prompt,
                    draft.target_count, draft.depends_on_task_id, max_retry,
                    fm_tab, fm_subtab, fm_aspect, fm_output, fm_duration, fm_model,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise TaskRepositoryError(
                f"failed to insert task_id={task_id}: {exc}"
            ) from exc

        for order_idx, (path, kind) in enumerate(final_assets, start=1):
            conn.execute(
                """
                INSERT INTO task_assets (task_id, asset_order, asset_path, asset_type)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, order_idx, str(path), kind),
            )
    return task_id


def _row_to_record(row: sqlite3.Row) -> TaskRecord:
    flow_fields = {
        "tab": row["flow_mode_tab"],
        "subtab": row["flow_mode_subtab"],
        "aspect": row["flow_mode_aspect"],
        "output_count": row["flow_mode_output_count"],
        "duration_sec": row["flow_mode_duration_sec"],
        "model": row["flow_mode_model"],
    }
    flow_mode = (
        FlowModeSpec(**flow_fields)
        if any(v is not None for v in flow_fields.values()) else None
    )
    return TaskRecord(
        task_id=row["task_id"],
        sku_id=row["sku_id"],
        creative_id=row["creative_id"],
        segment_id=row["segment_id"],
        sequence_index=row["sequence_index"],
        video_prompt=row["video_prompt"],
        target_count=row["target_count"],
        downloaded_count=row["downloaded_count"],
        generation_round_count=row["generation_round_count"],
        status=row["status"],
        assigned_workstation_id=row["assigned_workstation_id"],
        retry_count=row["retry_count"],
        max_retry_count=row["max_retry_count"],
        depends_on_task_id=row["depends_on_task_id"],
        source_asset_path=row["source_asset_path"],
        source_asset_type=row["source_asset_type"],
        error_type=row["error_type"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        flow_mode=flow_mode,
    )


_LIST_COLUMNS = (
    "task_id, sku_id, creative_id, segment_id, sequence_index, "
    "source_asset_path, source_asset_type, video_prompt, target_count, "
    "downloaded_count, generation_round_count, status, "
    "assigned_workstation_id, retry_count, max_retry_count, "
    "depends_on_task_id, error_type, error_message, created_at, "
    "flow_mode_tab, flow_mode_subtab, flow_mode_aspect, "
    "flow_mode_output_count, flow_mode_duration_sec, flow_mode_model"
)


def list_tasks(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int | None = None,
) -> list[TaskRecord]:
    """List tasks, optionally filtered by status. Active tasks (running /
    pending / retry_waiting) sort to the top so the dashboard's recent
    panel shows live work even when many older terminal tasks exist."""
    sql = f"SELECT {_LIST_COLUMNS} FROM tasks"
    params: list = []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status)
    sql += (
        " ORDER BY CASE status"
        "   WHEN 'running' THEN 0"
        "   WHEN 'pending' THEN 1"
        "   WHEN 'retry_waiting' THEN 2"
        "   WHEN 'manual_review' THEN 3"
        "   WHEN 'success' THEN 4"
        "   WHEN 'failed' THEN 5"
        "   WHEN 'download_failed' THEN 6"
        "   ELSE 7"
        " END, created_at DESC, task_id DESC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row_to_record(r) for r in conn.execute(sql, params).fetchall()]


def get_task(conn: sqlite3.Connection, task_id: str) -> TaskRecord | None:
    row = conn.execute(
        f"SELECT {_LIST_COLUMNS} FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    return _row_to_record(row) if row is not None else None


def get_task_assets(
    conn: sqlite3.Connection, task_id: str
) -> list[tuple[int, str, str]]:
    """Return ``[(order, path, kind), ...]`` ordered by asset_order ASC."""
    rows = conn.execute(
        "SELECT asset_order, asset_path, asset_type FROM task_assets "
        "WHERE task_id = ? ORDER BY asset_order ASC",
        (task_id,),
    ).fetchall()
    return [(r["asset_order"], r["asset_path"], r["asset_type"]) for r in rows]


_RESUMABLE_STATUSES = ("retry_waiting", "failed", "download_failed", "manual_review")


def resume_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Reset a stuck task's retry counter and flip it back to ``pending``
    so the scheduler can claim it again. Preserves ``downloaded_count``
    and ``generation_round_count`` so the next run continues from where
    the prior attempts left off — the customer-facing "继续任务" action.

    Also zeroes the auto-resume counter so the task gets a fresh budget
    of automatic retries the next time the scheduler picks it up.

    Returns True if the task was eligible (its status was non-terminal).
    Refuses to touch ``running`` tasks (would race the worker thread)
    or ``success`` tasks (nothing to resume).
    """
    row = conn.execute(
        "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    if row["status"] not in _RESUMABLE_STATUSES:
        return False
    with transaction(conn):
        conn.execute(
            """
            UPDATE tasks SET
                status = 'pending',
                retry_count = 0,
                auto_resumed_count = 0,
                error_type = NULL,
                error_message = NULL,
                assigned_workstation_id = NULL,
                started_at = NULL,
                finished_at = NULL
             WHERE task_id = ?
            """,
            (task_id,),
        )
    return True


def auto_resume_exhausted_tasks(
    conn: sqlite3.Connection,
    *,
    max_auto_resume_count: int,
) -> int:
    """Auto-resume tasks parked at retry exhaustion when there's likely
    something useful for them to do. Returns the number resumed.

    Eligibility:
    * status == 'retry_waiting' AND retry_count >= max_retry_count
    * auto_resumed_count < max_auto_resume_count (cap stops infinite
      loops when Flow's rate limit stays sticky)
    * at least one healthy workstation exists in the DB (otherwise the
      auto-resumed task would just go back to retry_waiting on the next
      pass)

    Customer can break out of the loop by clicking "继续任务" which
    resets ``auto_resumed_count`` to 0 (granting a fresh budget).
    """
    if max_auto_resume_count <= 0:
        return 0
    has_healthy = conn.execute(
        "SELECT 1 FROM workstations WHERE status = 'healthy' LIMIT 1"
    ).fetchone()
    if has_healthy is None:
        return 0
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE tasks SET
                status = 'pending',
                retry_count = 0,
                auto_resumed_count = auto_resumed_count + 1,
                error_type = NULL,
                error_message = NULL,
                assigned_workstation_id = NULL,
                started_at = NULL,
                finished_at = NULL
             WHERE status = 'retry_waiting'
               AND retry_count >= max_retry_count
               AND auto_resumed_count < ?
            """,
            (max_auto_resume_count,),
        )
    return cursor.rowcount


def delete_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    force: bool = False,
    remove_assets: bool = True,
) -> bool:
    """Delete a task row + its task_assets (CASCADE) + task_results (CASCADE).

    By default refuses to delete a task in ``running`` state — pass
    ``force=True`` to override. The on-disk asset directory is removed
    when ``remove_assets=True`` (default), but the generated mp4 / report
    files under ``output_root`` are left alone (per the existing data
    contract: persisted results stay even if the task row is gone).
    """
    row = conn.execute(
        "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    if row["status"] == "running" and not force:
        raise TaskRepositoryError(
            f"task {task_id} is currently running; pass force=True to delete"
        )
    with transaction(conn):
        conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))

    if remove_assets:
        asset_dir = paths.assets_dir() / task_id
        if asset_dir.exists():
            shutil.rmtree(asset_dir, ignore_errors=True)
    return True

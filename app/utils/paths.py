"""Output directory and file naming (T04).

Layout:

    output/{date}/{sku_id}/{creative_id}/segment_{segment_id}/
        {task_id}_round_{round}_seq_{seq}.mp4
        screenshots/
            {task_id}_round_{round}_{kind}.png

Path components are validated to reject path-traversal characters and empty
strings before any directory is created. ``creative_summary.md`` is just a
path predicate at MVP — the report module owns whether the file is produced.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path


_BAD = ("/", "\\", "..", "\x00")


def _validate_component(name: str, value: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    if value != value.strip():
        raise ValueError(f"{name} must not have leading/trailing whitespace: {value!r}")
    for token in _BAD:
        if token in value:
            raise ValueError(f"{name} contains illegal token {token!r}: {value!r}")
    return value


def segment_dir(
    output_root: Path | str,
    run_date: date | str,
    sku_id: str,
    creative_id: str,
    segment_id: str,
) -> Path:
    sku = _validate_component("sku_id", sku_id)
    creative = _validate_component("creative_id", creative_id)
    segment = _validate_component("segment_id", segment_id)
    if isinstance(run_date, date):
        date_str = run_date.isoformat()
    else:
        date_str = _validate_component("date", run_date)
    return Path(output_root) / date_str / sku / creative / f"segment_{segment}"


def screenshots_dir(seg_dir: Path) -> Path:
    return seg_dir / "screenshots"


def video_filename(
    task_id: str,
    generation_round: int,
    sequence_no: int,
    ext: str = ".mp4",
) -> str:
    _validate_component("task_id", task_id)
    if generation_round <= 0:
        raise ValueError(f"generation_round must be > 0, got {generation_round}")
    if sequence_no <= 0:
        raise ValueError(f"sequence_no must be > 0, got {sequence_no}")
    if not ext.startswith(".") or len(ext) < 2 or len(ext) > 6:
        raise ValueError(f"ext must look like '.mp4', got {ext!r}")
    return f"{task_id}_round_{generation_round:02d}_seq_{sequence_no:02d}{ext}"


# Map common media kinds to a default file extension. The Worker picks an
# extension upfront so persisted file paths are deterministic; the actual
# bytes get verified later by ``flow.download_candidate``.
_MEDIA_KIND_EXT = {
    "video": ".mp4",
    "image": ".png",
}


def candidate_extension(media_kind: str) -> str:
    return _MEDIA_KIND_EXT.get(media_kind, ".bin")


def screenshot_filename(task_id: str, generation_round: int, kind: str = "error") -> str:
    _validate_component("task_id", task_id)
    if generation_round <= 0:
        raise ValueError(f"generation_round must be > 0, got {generation_round}")
    if not kind:
        raise ValueError("kind must be non-empty")
    safe_kind = "".join(c for c in kind if c.isalnum() or c in "_-")
    return f"{task_id}_round_{generation_round:02d}_{safe_kind}.png"


def daily_report_path(output_root: Path | str, run_date: date | str) -> Path:
    if isinstance(run_date, date):
        date_str = run_date.isoformat()
    else:
        date_str = _validate_component("date", run_date)
    return Path(output_root) / date_str / "daily_report.md"


def ensure_segment_layout(
    output_root: Path | str,
    run_date: date | str,
    sku_id: str,
    creative_id: str,
    segment_id: str,
) -> Path:
    seg = segment_dir(output_root, run_date, sku_id, creative_id, segment_id)
    seg.mkdir(parents=True, exist_ok=True)
    screenshots_dir(seg).mkdir(parents=True, exist_ok=True)
    return seg

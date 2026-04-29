"""T04 — paths."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.utils.paths import (
    daily_report_path,
    ensure_segment_layout,
    screenshot_filename,
    segment_dir,
    video_filename,
)


def test_segment_dir_layout(tmp_path: Path) -> None:
    p = segment_dir(tmp_path, date(2026, 4, 28), "stroller_001", "stroller_001_creative_001", "A")
    assert p == tmp_path / "2026-04-28" / "stroller_001" / "stroller_001_creative_001" / "segment_A"


def test_video_filename_pattern() -> None:
    assert video_filename("T001", 1, 2) == "T001_round_01_seq_02.mp4"


def test_screenshot_filename_sanitizes_kind() -> None:
    name = screenshot_filename("T001", 2, "error/oops")
    assert name.startswith("T001_round_02_") and ".png" in name
    assert "/" not in name


def test_segment_dir_rejects_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        segment_dir(tmp_path, "2026-04-28", "sku", "creative", "../A")


def test_segment_dir_rejects_empty_segment(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        segment_dir(tmp_path, "2026-04-28", "sku", "creative", "")


def test_daily_report_path_is_sibling_of_sku(tmp_path: Path) -> None:
    p = daily_report_path(tmp_path, date(2026, 4, 28))
    assert p.name == "daily_report.md"
    assert p.parent == tmp_path / "2026-04-28"


def test_ensure_segment_layout_creates_dirs(tmp_path: Path) -> None:
    seg = ensure_segment_layout(tmp_path, "2026-04-28", "sku", "creative", "A")
    assert seg.exists()
    assert (seg / "screenshots").exists()

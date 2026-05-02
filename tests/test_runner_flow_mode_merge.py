"""Per-task FlowModeSpec override merging logic."""

from __future__ import annotations

from app.config.loader import FlowModeSpec
from app.runner.multi import _merge_flow_mode


def test_task_mode_overrides_workstation_field_by_field() -> None:
    ws = FlowModeSpec(
        tab="video", subtab="ingredients", aspect="9:16",
        output_count=1, duration_sec=8, model="Veo 3.1 - Fast",
    )
    task = FlowModeSpec(
        output_count=2, duration_sec=4, model="Veo 3.1 - Quality",
    )
    merged = _merge_flow_mode(ws, task)
    assert merged is not None
    assert merged.tab == "video"
    assert merged.subtab == "ingredients"
    assert merged.aspect == "9:16"
    assert merged.output_count == 2
    assert merged.duration_sec == 4
    assert merged.model == "Veo 3.1 - Quality"


def test_merge_no_task_returns_workstation() -> None:
    ws = FlowModeSpec(model="Veo 3.1 - Fast")
    assert _merge_flow_mode(ws, None) is ws


def test_merge_no_workstation_returns_task() -> None:
    task = FlowModeSpec(model="Veo 3.1 - Quality")
    assert _merge_flow_mode(None, task) is task


def test_merge_both_none_returns_none() -> None:
    assert _merge_flow_mode(None, None) is None


def test_task_field_none_falls_back_to_workstation() -> None:
    ws = FlowModeSpec(aspect="9:16", duration_sec=8)
    task = FlowModeSpec(aspect=None, duration_sec=4)
    merged = _merge_flow_mode(ws, task)
    assert merged is not None
    assert merged.aspect == "9:16"
    assert merged.duration_sec == 4

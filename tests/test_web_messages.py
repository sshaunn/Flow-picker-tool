"""Friendly Chinese error/cooldown labels."""

from __future__ import annotations

from app.web.messages import task_error_friendly, ws_cooldown_friendly


def test_task_error_known_returns_chinese() -> None:
    desc, suggestion = task_error_friendly("unusual_activity")
    assert "Google" in desc and "异常" in desc
    assert suggestion is not None and "冷却" in suggestion


def test_task_error_unknown_falls_through_to_raw() -> None:
    desc, suggestion = task_error_friendly("brand_new_error_code")
    assert desc == "brand_new_error_code"
    assert suggestion is None


def test_task_error_none_returns_none() -> None:
    assert task_error_friendly(None) is None
    assert task_error_friendly("") is None


def test_ws_cooldown_strike_n_includes_strike_number() -> None:
    desc, _ = ws_cooldown_friendly("unusual_activity_strike_3")
    assert "第 3 次" in desc


def test_ws_cooldown_strike_4_warns_about_next_tier() -> None:
    desc, suggestion = ws_cooldown_friendly("unusual_activity_strike_4")
    assert "上限" in (suggestion or "")


def test_ws_cooldown_consecutive_failure_known() -> None:
    desc, _ = ws_cooldown_friendly("consecutive_failure")
    assert "连续" in desc


def test_ws_cooldown_unknown_falls_through() -> None:
    desc, suggestion = ws_cooldown_friendly("brand_new_reason:exhausted")
    assert desc == "brand_new_reason:exhausted"
    assert suggestion is None

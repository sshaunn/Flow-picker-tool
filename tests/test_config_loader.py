"""T01 — config loader tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.loader import ConfigError, load_settings, load_workstations


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


VALID_SETTINGS = """
generation:
  max_round_per_task: 8
  max_retry_count: 2
  page_action_timeout_sec: 60
  generation_wait_timeout_sec: 600
cooldown:
  consecutive_failure_threshold: 3
  cooldown_duration_short_min: 30
  cooldown_duration_long_min: 60
  page_failure_window_min: 5
  page_failure_threshold: 3
flow:
  entry_url: "https://labs.google/flow"
"""


VALID_WS = """
workstations:
  - id: "WS_A"
    account_label: "acct_a"
    browser_profile_path: "./profiles/A"
    daily_task_limit: 20
    status: "healthy"
"""


def test_load_settings_parses_valid_yaml(tmp_path: Path) -> None:
    p = _write(tmp_path / "settings.yaml", VALID_SETTINGS)
    cfg = load_settings(p)
    assert cfg.generation.max_round_per_task == 8
    assert cfg.cooldown.consecutive_failure_threshold == 3
    assert cfg.flow.entry_url.startswith("https://")


def test_load_settings_returns_overridden_values(tmp_path: Path) -> None:
    overridden = VALID_SETTINGS.replace("max_round_per_task: 8", "max_round_per_task: 12")
    overridden = overridden.replace("max_retry_count: 2", "max_retry_count: 5")
    p = _write(tmp_path / "settings.yaml", overridden)
    cfg = load_settings(p)
    assert cfg.generation.max_round_per_task == 12
    assert cfg.generation.max_retry_count == 5


def test_load_settings_rejects_invalid_thresholds(tmp_path: Path) -> None:
    bad = VALID_SETTINGS.replace("max_round_per_task: 8", "max_round_per_task: 0")
    p = _write(tmp_path / "settings.yaml", bad)
    with pytest.raises(ConfigError):
        load_settings(p)


def test_load_settings_rejects_negative_max_retry(tmp_path: Path) -> None:
    bad = VALID_SETTINGS.replace("max_retry_count: 2", "max_retry_count: -1")
    p = _write(tmp_path / "settings.yaml", bad)
    with pytest.raises(ConfigError):
        load_settings(p)


def test_load_settings_rejects_negative_cooldown(tmp_path: Path) -> None:
    bad = VALID_SETTINGS.replace(
        "cooldown_duration_short_min: 30", "cooldown_duration_short_min: -5"
    )
    p = _write(tmp_path / "settings.yaml", bad)
    with pytest.raises(ConfigError):
        load_settings(p)


def test_load_settings_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.yaml")


def test_load_workstations_requires_profile_path(tmp_path: Path) -> None:
    bad = """
workstations:
  - id: "WS_A"
    account_label: "acct_a"
    daily_task_limit: 20
"""
    p = _write(tmp_path / "ws.yaml", bad)
    with pytest.raises(ConfigError):
        load_workstations(p)


def test_load_workstations_requires_daily_limit(tmp_path: Path) -> None:
    bad = """
workstations:
  - id: "WS_A"
    account_label: "acct_a"
    browser_profile_path: "./profiles/A"
"""
    p = _write(tmp_path / "ws.yaml", bad)
    with pytest.raises(ConfigError):
        load_workstations(p)


def test_load_workstations_rejects_duplicate_ids(tmp_path: Path) -> None:
    bad = VALID_WS + """
  - id: "WS_A"
    account_label: "acct_a2"
    browser_profile_path: "./profiles/A2"
    daily_task_limit: 20
"""
    p = _write(tmp_path / "ws.yaml", bad)
    with pytest.raises(ConfigError):
        load_workstations(p)


def test_load_workstations_valid(tmp_path: Path) -> None:
    p = _write(tmp_path / "ws.yaml", VALID_WS)
    ws = load_workstations(p)
    assert len(ws) == 1
    assert ws[0].id == "WS_A"

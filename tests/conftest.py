"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.loader import (
    AppConfig,
    CooldownSettings,
    FlowSettings,
    GenerationSettings,
    ModeProfile,
    OperationModeSettings,
    RecoverySettings,
    WorkstationConfig,
)
from app.db.schema import init_schema


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "flow_harvester.sqlite"
    init_schema(p)
    return p


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    p = tmp_path / "output"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def app_config(tmp_path: Path, db_path: Path, output_root: Path) -> AppConfig:
    log_root = tmp_path / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        generation=GenerationSettings(
            max_round_per_task=3,
            max_retry_count=2,
            page_action_timeout_sec=5,
            generation_wait_timeout_sec=5,
            # Disable stagger + inter-round pause so multi-runner tests
            # don't sleep their way past the test timeout. Production runs
            # set this to ~60s to space out per-account Veo requests; the
            # mock runner doesn't hit a live IP so spacing is irrelevant.
            inter_workstation_launch_stagger_sec=0,
            inter_round_pause_sec=0,
        ),
        cooldown=CooldownSettings(
            consecutive_failure_threshold=3,
            cooldown_duration_short_min=30,
            cooldown_duration_long_min=60,
            page_failure_window_min=5,
            page_failure_threshold=3,
        ),
        flow=FlowSettings(entry_url="http://localhost/mock"),
        recovery=RecoverySettings(running_stale_minutes=30, zombie_recovery_limit=3),
        # Override the day/night profiles too — the daemon now reads from
        # operation_modes per pass, not from generation settings, so we
        # need stagger=0 + high concurrency here as well.
        operation_modes=OperationModeSettings(
            day=ModeProfile(
                stagger_sec=0, max_concurrent_ws=10, auto_resume_cap=3,
                captcha_action="pause",
            ),
            night=ModeProfile(
                stagger_sec=0, max_concurrent_ws=10, auto_resume_cap=5,
                captcha_action="skip",
            ),
        ),
        output_root=str(output_root),
        db_path=str(db_path),
        log_root=str(log_root),
    )


@pytest.fixture
def workstations(tmp_path: Path) -> list[WorkstationConfig]:
    profiles_dir = tmp_path / "profiles"
    out: list[WorkstationConfig] = []
    for label in ("WS_A", "WS_B", "WS_C"):
        profile = profiles_dir / label
        profile.mkdir(parents=True, exist_ok=True)
        out.append(
            WorkstationConfig(
                id=label,
                account_label=f"acct_{label}",
                browser_profile_path=str(profile),
                daily_task_limit=20,
                status="healthy",
            )
        )
    return out


@pytest.fixture
def asset_root(tmp_path: Path) -> Path:
    images = tmp_path / "input" / "images"
    images.mkdir(parents=True, exist_ok=True)
    return tmp_path / "input"


def _write_image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return path


@pytest.fixture
def make_image():
    return _write_image

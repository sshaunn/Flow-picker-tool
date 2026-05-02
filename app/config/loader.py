"""Config loader with strict validation.

Reads ``settings.yaml`` and ``workstations.yaml`` and produces validated
config objects via pydantic. Invalid values must fail fast at CLI startup,
not at execution time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app import paths


class ConfigError(ValueError):
    """Raised when configuration files are missing fields or contain invalid values."""


class GenerationSettings(BaseModel):
    max_round_per_task: int = Field(8, gt=0)
    max_retry_count: int = Field(2, ge=0)
    page_action_timeout_sec: int = Field(60, gt=0)
    generation_wait_timeout_sec: int = Field(600, gt=0)
    # Stagger between workstation launches in the multi-runner.
    # Without this, N workstations all fire their first
    # ``trigger_generation`` within seconds of each other from the same
    # IP, which Google's Veo backend treats as a single bot fanning out
    # parallel requests and flags every workstation with unusual_activity
    # at once. A 60-90s stagger ensures only one Veo request is in flight
    # per IP at any moment, matching the natural pace of a human switching
    # between accounts.
    inter_workstation_launch_stagger_sec: int = Field(60, ge=0)
    # Pause between consecutive generation rounds inside the same task.
    # Spreads requests so Google's per-account rate-limit (the trigger
    # behind unusual_activity) is less likely to fire. Applied AFTER the
    # download of round N before starting round N+1; not applied before
    # round 1.
    inter_round_pause_sec: int = Field(5, ge=0)


class CooldownSettings(BaseModel):
    consecutive_failure_threshold: int = Field(3, gt=0)
    cooldown_duration_short_min: int = Field(30, gt=0)
    cooldown_duration_long_min: int = Field(60, gt=0)
    page_failure_window_min: int = Field(5, gt=0)
    page_failure_threshold: int = Field(3, gt=0)
    # Strike-based recovery for unusual_activity. The phrase is observed
    # to be intermittent (sticky for the account but auto-clears between
    # generations) rather than a hard ban. So the first hit doesn't go
    # straight to manual_check — instead we cool the workstation down for
    # the strike-N cooldown, let the next claimed task run, and only
    # escalate to manual_check after ``unusual_activity_max_strikes``
    # consecutive strikes (a successful generation resets the counter).
    #
    # Defaults: 30m / 1h / 2h / 4h cooldowns for strikes 1..4; strike 5
    # locks the workstation in manual_check for operator intervention.
    unusual_activity_max_strikes: int = Field(5, gt=0)
    unusual_activity_strike_cooldown_minutes: list[int] = Field(
        default_factory=lambda: [30, 60, 120, 240]
    )
    # Probe-based recovery for ``manual_check`` workstations (only the
    # rare hard-locked case after exhausting the strike budget). Probes
    # never burn a generation quota — they just check the page text.
    unusual_activity_probe_backoff_hours: list[int] = Field(
        default_factory=lambda: [4, 8, 24]
    )


class FlowSettings(BaseModel):
    entry_url: str = Field(..., min_length=1)


class RecoverySettings(BaseModel):
    running_stale_minutes: int = Field(30, gt=0)
    zombie_recovery_limit: int = Field(3, gt=0)


class AppConfig(BaseModel):
    generation: GenerationSettings
    cooldown: CooldownSettings
    flow: FlowSettings
    recovery: RecoverySettings = RecoverySettings()
    # Path defaults are platform-aware (see ``app.paths``). settings.yaml
    # may pin explicit values to override (the dev repo does this so test
    # output stays in-tree); customer installs leave these unset so the
    # app writes under %LOCALAPPDATA% / ~/Library/Application Support.
    output_root: str = Field(default_factory=lambda: str(paths.output_root()))
    db_path: str = Field(default_factory=lambda: str(paths.db_path()))
    log_root: str = Field(default_factory=lambda: str(paths.logs_dir()))


class FlowModeSpec(BaseModel):
    """Project-level UI state to assert before each generation round.

    Flow's project page does not always remember the previously-selected
    model / aspect / output count between sessions; the worker forces the
    state defined here on every ``open()`` so each task generates the kind
    of output the operator expects.

    All fields are optional — leave one ``None`` to leave Flow's current
    selection untouched.
    """

    tab: str | None = None              # 'video' | 'image'
    subtab: str | None = None           # 'ingredients' | 'frames'
    aspect: str | None = None           # '9:16' | '16:9' | '1:1'
    output_count: int | None = None     # 1..4
    duration_sec: int | None = None     # 4 | 6 | 8
    model: str | None = None            # substring match, e.g. 'Veo 3.1 - Fast'

    @field_validator("tab")
    @classmethod
    def _check_tab(cls, v):
        if v is not None and v not in {"video", "image"}:
            raise ValueError("tab must be 'video' or 'image'")
        return v

    @field_validator("subtab")
    @classmethod
    def _check_subtab(cls, v):
        if v is not None and v not in {"ingredients", "frames"}:
            raise ValueError("subtab must be 'ingredients' or 'frames'")
        return v

    @field_validator("aspect")
    @classmethod
    def _check_aspect(cls, v):
        if v is not None and v not in {"9:16", "16:9", "1:1"}:
            raise ValueError("aspect must be one of '9:16', '16:9', '1:1'")
        return v

    @field_validator("output_count")
    @classmethod
    def _check_output(cls, v):
        if v is not None and v not in {1, 2, 3, 4}:
            raise ValueError("output_count must be 1, 2, 3, or 4")
        return v

    @field_validator("duration_sec")
    @classmethod
    def _check_duration(cls, v):
        if v is not None and v not in {4, 6, 8}:
            raise ValueError("duration_sec must be 4, 6, or 8")
        return v


class WorkstationConfig(BaseModel):
    id: str = Field(..., min_length=1)
    account_label: str = Field(..., min_length=1)
    browser_profile_path: str = Field(..., min_length=1)
    daily_task_limit: int = Field(..., gt=0)
    status: str = "healthy"
    # Optional: pin this workstation to a specific Flow project URL. Required
    # for V0 because Flow's generation UI lives at /fx/tools/flow/project/<id>
    # and the homepage just shows the project list. Each account should have
    # its own dedicated project so candidates from different accounts don't
    # mingle in the same gallery.
    flow_project_url: str | None = None
    # Per-workstation Flow UI preset, asserted at the start of every run.
    flow_mode: FlowModeSpec | None = None

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        allowed = {"healthy", "busy", "cooldown", "manual_check", "disabled"}
        if v not in allowed:
            raise ValueError(f"workstation status must be one of {sorted(allowed)}, got {v!r}")
        return v


class WorkstationsFile(BaseModel):
    workstations: list[WorkstationConfig]

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "WorkstationsFile":
        seen: set[str] = set()
        for ws in self.workstations:
            if ws.id in seen:
                raise ValueError(f"duplicate workstation id: {ws.id}")
            seen.add(ws.id)
        return self


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        raise ConfigError(f"empty config file: {path}")
    if not isinstance(data, dict):
        raise ConfigError(f"top level of {path} must be a mapping")
    return data


def load_settings(path: Path | str) -> AppConfig:
    raw = _read_yaml(Path(path))
    try:
        return AppConfig(**raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid settings.yaml: {exc}") from exc


def load_workstations(path: Path | str) -> list[WorkstationConfig]:
    raw = _read_yaml(Path(path))
    try:
        parsed = WorkstationsFile(**raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid workstations.yaml: {exc}") from exc
    return parsed.workstations


def load_config(
    settings_path: Path | str = "config/settings.yaml",
    workstations_path: Path | str = "config/workstations.yaml",
) -> tuple[AppConfig, list[WorkstationConfig]]:
    return load_settings(settings_path), load_workstations(workstations_path)

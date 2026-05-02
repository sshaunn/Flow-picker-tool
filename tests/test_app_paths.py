"""Cross-platform app data path resolution (`app.paths`).

Distinct from ``tests/test_paths.py`` which covers the in-output filename
helpers (`app/utils/paths.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import paths


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop env overrides so each test starts from a clean slate."""
    for var in (
        paths._ENV_DATA_DIR,
        paths._ENV_OUTPUT_DIR,
        "LOCALAPPDATA",
        "USERPROFILE",
        "XDG_DATA_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


def test_macos_layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    expected_root = tmp_path / "Library" / "Application Support" / "FlowHarvester"
    assert paths.app_data_dir() == expected_root
    assert paths.output_root() == expected_root / "output"
    assert paths.profiles_dir() == expected_root / "profiles"
    assert paths.logs_dir() == expected_root / "logs"
    assert paths.db_path() == expected_root / "flow_harvester.sqlite"
    assert paths.config_dir() == expected_root / "config"


def test_windows_layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths.sys, "platform", "win32")
    localappdata = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert paths.app_data_dir() == localappdata / "FlowHarvester"
    # Output goes to Documents on Windows, NOT AppData — customer needs to
    # find generated mp4s in Explorer without spelunking hidden folders.
    assert paths.output_root() == tmp_path / "Documents" / "FlowHarvester" / "output"
    assert paths.profiles_dir() == localappdata / "FlowHarvester" / "profiles"
    assert paths.db_path() == localappdata / "FlowHarvester" / "flow_harvester.sqlite"


def test_windows_falls_back_when_localappdata_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.app_data_dir() == tmp_path / "AppData" / "Local" / "FlowHarvester"
    assert paths.output_root() == tmp_path / "Documents" / "FlowHarvester" / "output"


def test_linux_xdg_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert paths.app_data_dir() == tmp_path / "share" / "FlowHarvester"
    assert paths.output_root() == tmp_path / "share" / "FlowHarvester" / "output"


def test_linux_fallback_without_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.app_data_dir() == tmp_path / ".local" / "share" / "FlowHarvester"


def test_data_dir_env_override_wins_on_any_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    custom = tmp_path / "custom-data"
    monkeypatch.setenv(paths._ENV_DATA_DIR, str(custom))
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert paths.app_data_dir() == custom
    assert paths.profiles_dir() == custom / "profiles"
    assert paths.db_path() == custom / "flow_harvester.sqlite"


def test_output_dir_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    custom_out = tmp_path / "custom-output"
    monkeypatch.setenv(paths._ENV_OUTPUT_DIR, str(custom_out))
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # Output override is independent of app_data_dir.
    assert paths.output_root() == custom_out
    assert paths.app_data_dir() == tmp_path / "AppData" / "Local" / "FlowHarvester"


def test_workstation_profile_path_basic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.workstation_profile_path("WS_A") == paths.profiles_dir() / "WS_A"


@pytest.mark.parametrize("bad_id", ["", "../etc/passwd", "ws/a", "ws\\a"])
def test_workstation_profile_path_rejects_path_traversal(bad_id: str) -> None:
    with pytest.raises(ValueError):
        paths.workstation_profile_path(bad_id)


def test_ensure_app_dirs_creates_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(paths._ENV_DATA_DIR, str(tmp_path / "data"))
    monkeypatch.setenv(paths._ENV_OUTPUT_DIR, str(tmp_path / "out"))
    paths.ensure_app_dirs()
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "data" / "profiles").is_dir()
    assert (tmp_path / "data" / "logs").is_dir()
    assert (tmp_path / "data" / "config").is_dir()
    assert (tmp_path / "out").is_dir()


def test_app_config_defaults_use_paths_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When yaml omits output_root / db_path / log_root, AppConfig must
    pull from ``app.paths`` rather than the legacy hardcoded ``./output``.
    """
    monkeypatch.setenv(paths._ENV_DATA_DIR, str(tmp_path / "data"))
    monkeypatch.setenv(paths._ENV_OUTPUT_DIR, str(tmp_path / "out"))

    from app.config.loader import (
        AppConfig,
        CooldownSettings,
        FlowSettings,
        GenerationSettings,
    )

    cfg = AppConfig(
        generation=GenerationSettings(),
        cooldown=CooldownSettings(),
        flow=FlowSettings(entry_url="https://labs.google/fx/tools/flow"),
    )
    assert cfg.output_root == str(tmp_path / "out")
    assert cfg.db_path == str(tmp_path / "data" / "flow_harvester.sqlite")
    assert cfg.log_root == str(tmp_path / "data" / "logs")


def test_app_config_yaml_values_override_paths_defaults() -> None:
    """Explicit yaml-loaded values still win — dev workflow stays unchanged."""
    from app.config.loader import (
        AppConfig,
        CooldownSettings,
        FlowSettings,
        GenerationSettings,
    )

    cfg = AppConfig(
        generation=GenerationSettings(),
        cooldown=CooldownSettings(),
        flow=FlowSettings(entry_url="https://labs.google/fx/tools/flow"),
        output_root="./output",
        db_path="./flow_harvester.sqlite",
        log_root="./logs",
    )
    assert cfg.output_root == "./output"
    assert cfg.db_path == "./flow_harvester.sqlite"
    assert cfg.log_root == "./logs"

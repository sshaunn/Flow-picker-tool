"""T06 — profile check tests."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.workstations.profile_check import check_profile


def test_check_profile_ok(tmp_path: Path) -> None:
    profile = tmp_path / "ws"
    profile.mkdir()
    res = check_profile(profile)
    assert res.ok and res.reason is None


def test_check_profile_missing(tmp_path: Path) -> None:
    res = check_profile(tmp_path / "missing")
    assert not res.ok
    assert "does not exist" in (res.reason or "")


def test_check_profile_not_a_dir(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("hi")
    res = check_profile(f)
    assert not res.ok
    assert "not a directory" in (res.reason or "")


def test_check_profile_unwritable(tmp_path: Path) -> None:
    profile = tmp_path / "ws"
    profile.mkdir()
    # Make it read-only — note: skip if running as root.
    if os.geteuid() == 0:
        pytest.skip("running as root: chmod has no effect")
    profile.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        res = check_profile(profile)
        assert not res.ok
    finally:
        profile.chmod(stat.S_IRWXU)

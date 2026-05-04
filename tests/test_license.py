"""License key signing + verification tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.license import (
    LicenseError,
    LicensePayload,
    generate_license,
    load_license_file,
    verify_license,
)


def test_generate_then_verify_round_trip() -> None:
    data = generate_license("acme-corp", days=30)
    payload = verify_license(data)
    assert payload.customer_id == "acme-corp"
    # Issued ~now, expires ~30 days later.
    now = datetime.now(timezone.utc)
    assert (now - payload.issued_at).total_seconds() < 60
    assert 29 <= payload.days_remaining <= 30


def test_verify_rejects_tampered_customer() -> None:
    data = generate_license("acme-corp", days=30)
    data["customer_id"] = "evil-corp"  # forge after signing
    with pytest.raises(LicenseError, match="签名"):
        verify_license(data)


def test_verify_rejects_extended_expiration() -> None:
    """Stretching expires_at after signing must invalidate the key."""
    data = generate_license("acme-corp", days=30)
    far_future = datetime.now(timezone.utc) + timedelta(days=3650)
    data["expires_at"] = far_future.isoformat()
    with pytest.raises(LicenseError, match="签名"):
        verify_license(data)


def test_verify_rejects_missing_signature() -> None:
    data = generate_license("acme-corp", days=30)
    del data["signature"]
    with pytest.raises(LicenseError, match="签名"):
        verify_license(data)


def test_verify_rejects_expired_license() -> None:
    """A correctly-signed but past-expiry key must be rejected."""
    issued = datetime.now(timezone.utc) - timedelta(days=60)
    expires = datetime.now(timezone.utc) - timedelta(days=30)
    data = {
        "customer_id": "acme-corp",
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
    }
    # Re-sign so signature is valid; only the expiration check should
    # reject this.
    from app.license import _sign  # type: ignore[attr-defined]
    data["signature"] = _sign(data)
    with pytest.raises(LicenseError, match="过期"):
        verify_license(data)


def test_verify_rejects_naive_datetime() -> None:
    """Issued/expired must be timezone-aware ISO timestamps."""
    issued = datetime.utcnow()  # naive
    expires = issued + timedelta(days=30)
    data = {
        "customer_id": "acme",
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
    }
    from app.license import _sign  # type: ignore[attr-defined]
    data["signature"] = _sign(data)
    with pytest.raises(LicenseError, match="时区"):
        verify_license(data)


def test_load_license_file_round_trip(tmp_path: Path) -> None:
    data = generate_license("acme-corp", days=7)
    path = tmp_path / "license.key"
    path.write_text(json.dumps(data), encoding="utf-8")
    payload = load_license_file(path)
    assert isinstance(payload, LicensePayload)
    assert payload.customer_id == "acme-corp"


def test_load_license_file_missing(tmp_path: Path) -> None:
    with pytest.raises(LicenseError, match="不存在"):
        load_license_file(tmp_path / "nope.key")


def test_load_license_file_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "license.key"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(LicenseError, match="无法读取"):
        load_license_file(path)


def test_generate_rejects_blank_customer() -> None:
    with pytest.raises(ValueError):
        generate_license("   ", days=30)


def test_generate_rejects_zero_days() -> None:
    with pytest.raises(ValueError):
        generate_license("acme", days=0)


def test_find_license_file_searches_app_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke-test the discovery path the bundle uses — make sure the
    paths.app_data_dir() lookup actually resolves (the original bug
    used a non-existent paths.data_dir()).
    """
    from app import paths
    from app.license import find_license_file

    monkeypatch.setenv(paths._ENV_DATA_DIR, str(tmp_path))
    # Nothing there → returns None.
    assert find_license_file() is None

    # Drop a key in the persistent location and confirm we find it.
    key_path = tmp_path / "license.key"
    key_path.write_text("{}", encoding="utf-8")
    found = find_license_file()
    assert found is not None
    assert found.resolve() == key_path.resolve()

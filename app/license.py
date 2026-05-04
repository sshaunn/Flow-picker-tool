"""Time-limited license check for the bundled customer install.

Bundled exe refuses to start if no valid ``license.key`` is found or
the embedded ``expires_at`` has passed. The dev signs each customer's
key with ``flow-harvester gen-license``; the customer drops the key
next to the exe (or under ``%LOCALAPPDATA%\\FlowHarvester\\``) and the
app verifies on startup.

Threat model: this is a deterrent against casual tampering, not real
anti-piracy. The HMAC secret below ships inside the bundled
``_internal/`` and a determined operator with reverse-engineering
skills could extract it and forge new keys. For real protection use
RSA + public-key verification.

License JSON shape::

    {
      "customer_id": "acme-corp",
      "issued_at": "2026-05-04T10:00:00+00:00",
      "expires_at": "2026-06-04T10:00:00+00:00",
      "signature": "<hex hmac-sha256 over the other 3 fields>"
    }
"""

from __future__ import annotations

import hmac
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# 32-byte HMAC secret. Rotating this invalidates every license issued
# previously, so don't change it without re-issuing keys.
_LICENSE_HMAC_SECRET = bytes.fromhex(
    "5362677d82e607e7323231815e27e72ef5adbc0faf795ff5174033d4592ceaf7"
)


class LicenseError(Exception):
    """Raised when the license is missing, malformed, signature
    mismatch, or expired. Caller surfaces this to the customer
    verbatim — keep messages user-friendly (Chinese)."""


@dataclass(frozen=True)
class LicensePayload:
    customer_id: str
    issued_at: datetime
    expires_at: datetime

    @property
    def days_remaining(self) -> int:
        """Whole days from now until expires_at. 0 if already expired
        (the verify step would have raised before this), guarded
        anyway."""
        delta = self.expires_at - datetime.now(timezone.utc)
        return max(0, delta.days)


def _canonical_payload(data: dict) -> bytes:
    """Serialize the signable fields in a stable order. Must be byte-
    for-byte identical between sign and verify or the HMAC won't
    match."""
    body = {k: data[k] for k in ("customer_id", "issued_at", "expires_at")}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign(data: dict) -> str:
    return hmac.new(
        _LICENSE_HMAC_SECRET, _canonical_payload(data), hashlib.sha256,
    ).hexdigest()


def generate_license(customer_id: str, days: int) -> dict:
    """Build a signed license dict ready for ``json.dump`` to a file.

    Used by ``flow-harvester gen-license`` in dev — never called from
    bundled code (the secret would still be in the bundle anyway, but
    we don't expose the entry point to customers).
    """
    customer_id = customer_id.strip()
    if not customer_id:
        raise ValueError("customer_id is required")
    if days <= 0:
        raise ValueError("days must be positive")
    issued = datetime.now(timezone.utc).replace(microsecond=0)
    expires = issued + timedelta(days=days)
    data = {
        "customer_id": customer_id,
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
    }
    data["signature"] = _sign(data)
    return data


def verify_license(data: dict) -> LicensePayload:
    """Validate signature + expiration, return the parsed payload.

    Raises ``LicenseError`` (not ValueError) so the bundle entry point
    can catch a single exception type for its crash dialog.
    """
    sig = data.get("signature")
    if not isinstance(sig, str) or not sig:
        raise LicenseError("授权文件格式错误：缺少签名")
    for key in ("customer_id", "issued_at", "expires_at"):
        if key not in data:
            raise LicenseError(f"授权文件格式错误：缺少 {key}")
    expected = _sign(data)
    if not hmac.compare_digest(sig, expected):
        raise LicenseError("授权文件签名无效（可能被改过或非本工具发放）")
    try:
        issued = datetime.fromisoformat(data["issued_at"])
        expires = datetime.fromisoformat(data["expires_at"])
    except (TypeError, ValueError) as exc:
        raise LicenseError(f"授权文件日期字段格式错误：{exc}") from exc
    if expires.tzinfo is None or issued.tzinfo is None:
        raise LicenseError("授权文件日期必须带时区")
    now = datetime.now(timezone.utc)
    if now >= expires:
        raise LicenseError(
            f"授权已于 {expires.astimezone().strftime('%Y-%m-%d %H:%M')} 过期，"
            "请联系开发者续期"
        )
    return LicensePayload(
        customer_id=str(data["customer_id"]),
        issued_at=issued,
        expires_at=expires,
    )


def load_license_file(path: Path) -> LicensePayload:
    """Read + verify a license.key file. Single entry point for the
    bundle startup check."""
    if not path.exists():
        raise LicenseError(f"授权文件不存在：{path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise LicenseError(f"授权文件无法读取：{exc}") from exc
    if not isinstance(data, dict):
        raise LicenseError("授权文件内容必须是 JSON 对象")
    return verify_license(data)


def find_license_file() -> Optional[Path]:
    """Search the conventional locations for ``license.key`` and
    return the first that exists. None if none found.

    Order — operator-managed location first so a fresh bundle drop
    doesn't overwrite an active key:

      1. ``%LOCALAPPDATA%\\FlowHarvester\\license.key`` (persistent)
      2. Bundled ``_MEIPASS / license.key`` (PyInstaller datas)
      3. Same dir as exe / project root
      4. ``_internal/license.key`` next to the exe
      5. CWD
    """
    from app import paths
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        try:
            r = p.resolve()
        except (OSError, RuntimeError):
            r = p
        if r not in seen:
            seen.add(r)
            candidates.append(p)

    _add(paths.app_data_dir() / "license.key")
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        _add(Path(meipass) / "license.key")
    exe_dir = Path(sys.executable).resolve().parent
    _add(exe_dir / "license.key")
    _add(exe_dir / "_internal" / "license.key")
    _add(Path.cwd() / "license.key")
    for c in candidates:
        if c.exists():
            return c
    return None

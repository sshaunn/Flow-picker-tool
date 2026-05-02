"""Probe whether a workstation profile is signed into Flow.

Used by:

* ``check-login`` CLI — silently inspects all profiles and prints which ones
  still need a human login pass.
* ``login-flow --all`` — skips profiles that already have a valid session
  so an operator only clicks through the ones that actually need it.

Detection is selector-based, not network-based: we navigate to the Flow
entry URL with a *headless* persistent context and look at:

1. URL (post-redirect) — if it lands on accounts.google.com or
   labs.google/flow/about, we're definitely not signed in.
2. The presence of the ``prompt_input`` selector from
   ``flow-selectors.yaml`` — that selector is present only inside the
   actual generation app, so finding it means cookies are valid.
3. Body text — ``Sign in`` / ``unusual activity`` phrases force a "needs
   action" verdict.

The probe never types credentials and never auto-dismisses anything; it's a
read-only check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.worker.flow_selectors import FlowSelectorsConfig, load_flow_selectors


_LOG = logging.getLogger("flow_harvester.login_status")


class LoginStatus(str, Enum):
    LOGGED_IN = "logged_in"
    NEEDS_LOGIN = "needs_login"
    NEEDS_MANUAL_CHECK = "needs_manual_check"  # captcha / unusual activity
    PROFILE_MISSING = "profile_missing"
    PROBE_FAILED = "probe_failed"


@dataclass
class LoginProbeResult:
    workstation_id: str
    status: LoginStatus
    final_url: str
    detail: str


def _phrase_match(haystack: str, phrases: list[str]) -> bool:
    if not haystack:
        return False
    lower = haystack.lower()
    return any(p.lower() in lower for p in phrases)


def probe_workstation(
    *,
    workstation_id: str,
    profile_path: Path,
    entry_url: str,
    project_url: str | None = None,
    selectors_cfg: FlowSelectorsConfig | None = None,
    headless: bool = True,
    timeout_sec: int = 30,
) -> LoginProbeResult:
    if selectors_cfg is None:
        selectors_cfg = load_flow_selectors()

    if not profile_path.exists() or not profile_path.is_dir():
        return LoginProbeResult(
            workstation_id=workstation_id,
            status=LoginStatus.PROFILE_MISSING,
            final_url="",
            detail=f"profile dir missing: {profile_path}",
        )

    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        return LoginProbeResult(
            workstation_id=workstation_id,
            status=LoginStatus.PROBE_FAILED,
            final_url="",
            detail="patchright not installed",
        )

    try:
        with sync_playwright() as pw:
            # Match the launch shape used by ``PlaywrightFlowPort.open()``:
            # patchright + system Chrome + minimal customisation. Adding
            # custom UA / args / init scripts here triggered ERR_CONNECTION_
            # CLOSED in the wild — patchright handles its own anti-detection
            # patches and stacking ours on top breaks the launch.
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                channel="chrome",
                headless=headless,
                no_viewport=True,
                chromium_sandbox=True,
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(timeout_sec * 1000)
            # Prefer the project URL — that's the page that actually renders
            # the Slate prompt editor. The entry/tools URL only shows a
            # project picker, where prompt_input is missing whether logged
            # in or not, leading to false "needs_login" verdicts.
            target_url = project_url or entry_url
            try:
                page.goto(target_url, wait_until="domcontentloaded")
                # Allow the SPA to finish booting; avoids a false "needs login".
                page.wait_for_timeout(3_000)
            except Exception as exc:
                ctx.close()
                return LoginProbeResult(
                    workstation_id=workstation_id,
                    status=LoginStatus.PROBE_FAILED,
                    final_url=page.url or "",
                    detail=f"goto failed: {exc}",
                )

            url = page.url or ""
            try:
                body = (page.locator("body").inner_text(timeout=5_000) or "")[:4000]
            except Exception:
                body = ""

            # 1) Hard signals from URL.
            if "accounts.google.com" in url:
                ctx.close()
                return LoginProbeResult(
                    workstation_id, LoginStatus.NEEDS_LOGIN, url,
                    "redirected to accounts.google.com",
                )
            if "/flow/about" in url:
                ctx.close()
                return LoginProbeResult(
                    workstation_id, LoginStatus.NEEDS_LOGIN, url,
                    "landed on marketing page (/flow/about)",
                )

            # 2) unusual activity / captcha / verify => manual check.
            #    Service-level phrases are checked higher up by the runner;
            #    here we only care about hard account-level signals.
            phrases = selectors_cfg.state_phrases
            if _phrase_match(body, phrases.unusual_activity):
                ctx.close()
                return LoginProbeResult(
                    workstation_id, LoginStatus.NEEDS_MANUAL_CHECK, url,
                    "unusual_activity phrase detected",
                )
            if _phrase_match(body, phrases.captcha_or_verification):
                ctx.close()
                return LoginProbeResult(
                    workstation_id, LoginStatus.NEEDS_MANUAL_CHECK, url,
                    "captcha/verification phrase detected",
                )

            # 3) The presence of the prompt input is the canonical "logged in"
            #    signal — it only renders inside the generation app.
            try:
                has_prompt = page.locator(selectors_cfg.selectors.prompt_input).count() > 0
            except Exception:
                has_prompt = False

            ctx.close()
            if has_prompt:
                return LoginProbeResult(
                    workstation_id, LoginStatus.LOGGED_IN, url, "prompt input visible",
                )

            # 4) Fallback: signed-in phrase in body without prompt is suspicious.
            if _phrase_match(body, phrases.login_required):
                return LoginProbeResult(
                    workstation_id, LoginStatus.NEEDS_LOGIN, url,
                    "login phrase visible and prompt input absent",
                )

            return LoginProbeResult(
                workstation_id, LoginStatus.NEEDS_LOGIN, url,
                "prompt input not found",
            )
    except Exception as exc:
        _LOG.warning("probe failed for ws=%s: %s", workstation_id, exc)
        return LoginProbeResult(
            workstation_id=workstation_id,
            status=LoginStatus.PROBE_FAILED,
            final_url="",
            detail=str(exc),
        )

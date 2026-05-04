"""Frames-mode upload routing tests.

These exercise the kind-routing decision in
``PlaywrightFlowPort.upload_source_assets`` and the validation in
``_upload_via_frame_buttons`` without driving a real browser.

The full end-to-end click flow lives in test_flow_playwright_local.py
which requires Chromium.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _build_port_skeleton():
    """Construct a PlaywrightFlowPort instance bypassing patchright init.

    We only exercise the pure-Python routing / validation that runs
    before the first DOM interaction, so all browser plumbing stays
    out of scope. Replace ``self._page`` with a sentinel that fails
    if accidentally touched.
    """
    from app.worker.flow_playwright import PlaywrightFlowPort
    from app.worker.flow_selectors import (
        FlowSelectorsConfig, ModeControls, Selectors, StatePhrases, Timeouts,
    )

    port = PlaywrightFlowPort.__new__(PlaywrightFlowPort)
    port._cfg = FlowSelectorsConfig(
        entry_url_pattern=".*",
        state_phrases=StatePhrases(),
        selectors=Selectors(
            upload_button="input[type=file]",
            prompt_input="textarea",
            generate_button="button",
            candidate_items="video",
            prompt_attach_button="button.+",
            prompt_attach_dialog="div.dlg",
            prompt_attach_upload_target="text=Upload image",
            prompt_attach_button_start="button:text-is('Start')",
            prompt_attach_button_end="button:text-is('End')",
        ),
        timeouts=Timeouts(),
        mode_controls=ModeControls(),
    )
    # Fail loudly if the routing logic accidentally touches the page.
    port._page = MagicMock(name="must-not-touch")
    port._page.locator.side_effect = AssertionError(
        "routing logic should reject before any DOM call"
    )
    port.page_action_timeout_sec = 60
    return port


def _asset(name: str, kind: str, order: int = 1):
    from app.worker.flow_port import SourceAsset
    return SourceAsset(path=Path(f"/tmp/{name}"), kind=kind, order=order)


def test_frames_routing_requires_at_least_one_frame_kind():
    """Empty list is rejected by the outer guard, not the frames branch."""
    from app.worker.flow_port import FlowPortError
    port = _build_port_skeleton()
    with pytest.raises(FlowPortError, match="empty list"):
        port.upload_source_assets([])


def test_frames_branch_rejects_two_first_frames():
    """Two first_frame assets is ambiguous — refuse rather than guess."""
    from app.worker.flow_port import FlowPortError
    port = _build_port_skeleton()
    assets = [
        _asset("a.png", "first_frame", 1),
        _asset("b.png", "first_frame", 2),
    ]
    with pytest.raises(FlowPortError, match="exactly one first_frame"):
        port.upload_source_assets(assets)


def test_frames_branch_rejects_two_last_frames():
    from app.worker.flow_port import FlowPortError
    port = _build_port_skeleton()
    assets = [
        _asset("a.png", "last_frame", 1),
        _asset("b.png", "last_frame", 2),
    ]
    with pytest.raises(FlowPortError, match="exactly one last_frame"):
        port.upload_source_assets(assets)


def test_frames_branch_rejects_mixed_kinds():
    """Mixing first_frame + reference picks the frames branch (because
    of first_frame) but then must reject because reference isn't a
    valid frames-mode kind."""
    from app.worker.flow_port import FlowPortError
    port = _build_port_skeleton()
    assets = [
        _asset("a.png", "first_frame", 1),
        _asset("b.png", "reference", 2),
    ]
    with pytest.raises(FlowPortError, match="unsupported kind"):
        port.upload_source_assets(assets)


def test_ingredients_routing_does_not_trigger_frames_branch():
    """Pure reference assets must NOT take the frames branch — they
    should fall through to ``_upload_via_prompt_attach`` which clicks
    the ``+`` button. The page mock raises before the click lands; the
    flow_playwright code wraps that in FlowPortError with ``'+'`` in
    the message, distinguishing it from the frames-branch error which
    would mention ``'Start'`` or ``'End'``."""
    from app.worker.flow_port import FlowPortError
    port = _build_port_skeleton()
    assets = [_asset("a.png", "reference", 1)]
    with pytest.raises(FlowPortError, match=r"prompt-attach '\+' for asset"):
        port.upload_source_assets(assets)


def test_frames_missing_selectors_raises_friendly_error():
    """If the yaml didn't configure prompt_attach_button_start/end,
    the routing layer must say so instead of hitting None.click()."""
    from app.worker.flow_port import FlowPortError
    port = _build_port_skeleton()
    port._cfg.selectors.prompt_attach_button_start = ""
    port._cfg.selectors.prompt_attach_button_end = ""
    assets = [
        _asset("a.png", "first_frame", 1),
        _asset("b.png", "last_frame", 2),
    ]
    with pytest.raises(FlowPortError, match="frames mode upload requires"):
        port.upload_source_assets(assets)

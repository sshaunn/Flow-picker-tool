"""Flow selectors loader.

Reads ``config/flow-selectors.yaml`` and exposes typed objects so the
Playwright adapter and CLI helpers share one source of truth. The YAML
fields are validated at load time so an obviously broken config fails
before a browser is even launched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError


class StatePhrases(BaseModel):
    unusual_activity: list[str] = Field(default_factory=list)
    login_required: list[str] = Field(default_factory=list)
    captcha_or_verification: list[str] = Field(default_factory=list)
    page_load_failed: list[str] = Field(default_factory=list)
    service_unavailable: list[str] = Field(default_factory=list)
    # Per-generation transient errors (e.g. Veo audio sub-model failure)
    # that should NOT halt the workstation — the worker retries within
    # the same round (optionally after toggling a Flow setting).
    generation_retryable: list[str] = Field(default_factory=list)
    # Account-level: Flow says "you don't have access" (subscription
    # expired / region restricted / account flagged for repeat
    # automation abuse). Binary state — no cooldown / strike helps.
    # WS goes straight to manual_check.
    no_flow_access: list[str] = Field(default_factory=list)


class Selectors(BaseModel):
    upload_button: str
    prompt_input: str
    generate_button: str
    candidate_items: str
    # Regex on a candidate's ``src`` attribute to confirm it really is a
    # generated media URL (not a UI thumbnail / decorative video). Default
    # matches Flow's ``media.getMediaUrlRedirect`` tRPC endpoint.
    candidate_src_pattern: str = "media\\.getMediaUrlRedirect"

    # Prompt-attach upload selectors. When all three are configured, the
    # adapter uploads via "+ button → picker dialog → Upload image → file
    # chooser" so images become chips in the prompt itself (not separate
    # Ingredients reference uploads).
    prompt_attach_button: str = ""
    prompt_attach_dialog: str = ""
    prompt_attach_upload_target: str = ""

    # Frames sub-tab variants of the trigger button. Same dialog + upload
    # target as ingredients; only the trigger differs (two slots labelled
    # ``Start`` and ``End`` instead of a single ``+``). Both empty falls
    # back to ingredients-style upload.
    prompt_attach_button_start: str = ""
    prompt_attach_button_end: str = ""

    # Legacy fields — retained only so older flow-selectors.yaml files keep
    # validating. The adapter does not use them.
    generation_complete_marker: str = ""
    candidate_download_button: str = ""


class Timeouts(BaseModel):
    page_action: int = 60
    generation_complete: int = 600
    download: int = 120


class ModeControls(BaseModel):
    """Selectors used to force Flow's project UI to a known state.

    All fields are optional; missing entries simply mean "don't try to
    enforce that part of the UI". Templated entries (those containing
    ``{N}`` / ``{S}`` / ``{NAME}``) are formatted at use-time.
    """

    # Multiple fallbacks for the settings panel trigger (matches whichever
    # model name is currently displayed). The first present + visible
    # match wins.
    settings_trigger: list[str] = Field(default_factory=list)

    tab_video: str | None = None
    tab_image: str | None = None
    subtab_ingredients: str | None = None
    subtab_frames: str | None = None
    aspect_9_16: str | None = None
    aspect_16_9: str | None = None
    aspect_1_1: str | None = None
    output_count_template: str | None = None
    duration_template: str | None = None
    model_dropdown_button: str | None = None
    model_keywords: list[str] = Field(default_factory=list)
    model_menu_item_template: str | None = None


class FlowSelectorsConfig(BaseModel):
    entry_url_pattern: str
    account_language: str = "en-US"
    state_phrases: StatePhrases
    selectors: Selectors
    timeouts: Timeouts
    mode_controls: ModeControls = ModeControls()
    # Buttons to click if they appear on page open (cookie banners, TOS
    # modals, etc.). Tried in order; first visible match is clicked.
    popup_dismiss_buttons: list[str] = Field(default_factory=list)


_DEFAULT_SELECTORS_REL = "config/flow-selectors.yaml"


def _resolve_selectors_path(path: Path | str | None) -> Path:
    """Find the selectors yaml across dev / PyInstaller layouts.

    Priority:
    1. Explicit caller-supplied path (when truthy and exists).
    2. ``FLOW_HARVESTER_SELECTORS_YAML`` env var (set by the bundled
       ``__main__.py`` after it locates the file).
    3. ``config/flow-selectors.yaml`` relative to cwd (dev path).
    """
    if path:
        candidate = Path(path)
        if candidate.exists():
            return candidate
    import os
    env = os.environ.get("FLOW_HARVESTER_SELECTORS_YAML")
    if env:
        candidate = Path(env)
        if candidate.exists():
            return candidate
    return Path(_DEFAULT_SELECTORS_REL)


def load_flow_selectors(
    path: Path | str | None = None,
) -> FlowSelectorsConfig:
    p = _resolve_selectors_path(path)
    if not p.exists():
        raise FileNotFoundError(f"flow-selectors.yaml not found: {p}")
    raw: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"top level of {p} must be a mapping")
    try:
        return FlowSelectorsConfig(**raw)
    except ValidationError as exc:
        raise ValueError(f"invalid {p}: {exc}") from exc

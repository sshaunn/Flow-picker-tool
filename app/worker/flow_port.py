"""Flow page adapter port (T07).

The MVP separates *what* the runner needs from the page (open, classify state,
upload, paste, generate, list candidates, download) from *how* it is done
(Playwright, mock, future puppeteer port). The Worker only depends on
``FlowPort``; concrete implementations live in ``flow_playwright`` and
``flow_mock``.

Selector strings & DOM details belong in ``config/flow-selectors.yaml``
(loaded by the Playwright impl), not in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Protocol


class PageState(str, Enum):
    READY = "ready"
    UNUSUAL_ACTIVITY = "unusual_activity"
    LOGIN_REQUIRED = "login_required"
    CAPTCHA_OR_VERIFICATION = "captcha_or_verification"
    PAGE_LOAD_FAILED = "page_load_failed"
    # Flow itself is rate-limited / overloaded ("high demand", quota exceeded).
    # Service-level, not workstation-level — back off and retry on this WS later.
    SERVICE_UNAVAILABLE = "service_unavailable"
    # Account-level Flow access denied: "It looks like you don't have
    # access to Flow." This is binary (subscription expired / region
    # not supported / Google revoked Flow access for repeat
    # automation abuse). Cooldown won't help — strike escalation is
    # skipped, the WS goes straight to manual_check so the operator
    # can fix the subscription or swap the account.
    NO_FLOW_ACCESS = "no_flow_access"


@dataclass
class CandidateMeta:
    """A single candidate Flow produced this round.

    ``download_handle`` is whatever the adapter needs to actually download
    the file (a Playwright Locator, a URL, a mock path…). It's opaque to
    the worker — the worker just calls ``flow.download_candidate(...)``.

    ``media_kind`` lets the worker pick a sensible filename extension
    (e.g. ``.mp4`` for ``video``, ``.png`` for ``image``) without parsing
    the response. Defaults to ``video`` for back-compat with older mocks.
    """

    sequence_no: int
    download_handle: object
    media_kind: str = "video"  # 'video' | 'image'


@dataclass
class GenerationRoundResult:
    state: PageState
    candidates: list[CandidateMeta] = field(default_factory=list)
    error_message: str | None = None
    timed_out: bool = False


class FlowPortError(RuntimeError):
    """Raised by adapters for unrecoverable internal errors."""


@dataclass
class SourceAsset:
    """A single ordered source asset for a task.

    ``order`` is 1-based and matches ``task_assets.asset_order``. ``kind``
    is the semantic role (``first_frame``, ``last_frame``, ``reference``,
    etc.) and may be used by the adapter to pick the right upload slot
    (e.g. Frames sub-tab has separate first/last targets).
    """

    path: Path
    kind: str
    order: int


class FlowPort(Protocol):
    """Port the Worker drives during candidate generation."""

    def open(self) -> PageState: ...

    def upload_source_assets(self, assets: list[SourceAsset]) -> None: ...

    def paste_prompt(self, prompt: str) -> None: ...

    def trigger_generation(self) -> None: ...

    def wait_for_round_complete(
        self, timeout_sec: int, *, expected_count: Optional[int] = None,
    ) -> GenerationRoundResult: ...

    def download_candidate(self, candidate: CandidateMeta, target_path: Path) -> None: ...

    def take_screenshot(self, target_path: Path) -> None: ...

    def close(self) -> None: ...

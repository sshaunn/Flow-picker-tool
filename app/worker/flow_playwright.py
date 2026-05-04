"""Playwright-backed FlowPort.

Concrete implementation that drives Google Flow with a persistent browser
profile (so logged-in cookies survive between runs). Selectors and exception
phrases come from ``config/flow-selectors.yaml`` — when Flow's DOM moves,
*only that file* needs to change.

Round detection
---------------

Flow does NOT expose a "generation complete" DOM marker. Instead each
candidate appears as a ``<video>`` inside Flow's virtualized list, with the
``src`` attribute pointing at a tRPC redirect endpoint
(``/fx/api/trpc/media.getMediaUrlRedirect?name=<uuid>``). The historical
list contains every video the user has ever generated in this project, so
we cannot just count items — we *diff* against a pre-trigger snapshot.

The flow is:

* ``trigger_generation()``: take a snapshot of current candidate src URLs,
  then click Create.
* ``wait_for_round_complete()``: poll the candidate list every 2 s. When
  *new* src URLs appear, wait one more cycle to confirm the count is stable
  (so x4 outputs that arrive one-by-one all get collected). Return them.
* ``download_candidate()``: fetch the candidate's src URL via the
  authenticated Playwright request context — no button click required,
  cookies travel with the request automatically.

This is more reliable than DOM-button clicking because Flow's UI re-uses
DOM nodes (Virtuoso virtualization) and download buttons are buried inside
modals.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from app.worker.flow_port import (
    CandidateMeta,
    FlowPort,
    FlowPortError,
    GenerationRoundResult,
    PageState,
    SourceAsset,
)
from app.worker.flow_selectors import FlowSelectorsConfig, load_flow_selectors


_LOG = logging.getLogger("flow_harvester.playwright")


def _phrase_match(haystack: str, phrases: list[str]) -> bool:
    if not haystack:
        return False
    lower = haystack.lower()
    return any(p.lower() in lower for p in phrases)


def _phrase_match_which(haystack: str, phrases: list[str]) -> str | None:
    """Return the first phrase that matches ``haystack`` (lowercase
    substring), or None. Used by classifier for diagnostic logging so a
    false positive can be traced to the exact phrase that matched.
    """
    if not haystack:
        return None
    lower = haystack.lower()
    for p in phrases:
        if p.lower() in lower:
            return p
    return None


def _count_phrase_occurrences(haystack: str, phrases: list[str]) -> int:
    """Return total times any of ``phrases`` appears in ``haystack``.

    Used to distinguish a *new* unusual_activity event from a stale
    "Failed" card that Flow leaves in the project library — see
    ``_classify_state`` for the comparison logic.
    """
    if not haystack:
        return 0
    lower = haystack.lower()
    return sum(lower.count(p.lower()) for p in phrases)


# Flow's tRPC media URL: ``...media.getMediaUrlRedirect?name=<UUID>...``
# We dedup candidates by this UUID so that a poster ``<img>`` and the
# eventual ``<video>`` for the same generation collapse to one entry
# (with the ``<video>`` URL preferred — it serves the real mp4 bytes).
_NAME_PARAM_RE = re.compile(r"[?&]name=([\w-]+)")


def _spec_summary(spec) -> str:
    parts = []
    for f in ("tab", "subtab", "aspect", "output_count", "duration_sec", "model"):
        v = getattr(spec, f, None)
        if v is not None:
            parts.append(f"{f}={v}")
    return " ".join(parts) or "(empty)"


class PlaywrightFlowPort(FlowPort):
    def __init__(
        self,
        *,
        entry_url: str,
        profile_path: Path,
        page_action_timeout_sec: int = 60,
        selectors_path: Path | None = None,
        headless: bool = False,
        slow_mo_ms: int = 0,
        project_url: str | None = None,
        ensure_video_mode: bool = True,
        flow_mode_spec: object | None = None,
        candidate_stability_window_sec: float = 30.0,
    ) -> None:
        self.entry_url = entry_url
        self.profile_path = Path(profile_path)
        self.page_action_timeout_sec = page_action_timeout_sec
        self.selectors_path = selectors_path or Path("config/flow-selectors.yaml")
        self._headless = headless
        self._slow_mo_ms = slow_mo_ms
        self._cfg: FlowSelectorsConfig = load_flow_selectors(self.selectors_path)
        self._project_url = project_url
        self._ensure_video_mode = ensure_video_mode
        # ``flow_mode_spec`` is loosely typed (object) to avoid a circular
        # import with the config package; the runner passes a FlowModeSpec.
        self._flow_mode_spec = flow_mode_spec
        self._candidate_stability_window_sec = candidate_stability_window_sec

        # Lazily filled in open()
        self._pw = None  # patchright sync runtime
        self._context = None  # BrowserContext (persistent)
        self._page = None  # Page

        # Pre-trigger candidate snapshot (set of src URLs). Re-captured each
        # time ``trigger_generation`` is called so consecutive rounds within
        # the same task only see the latest round's outputs.
        self._candidate_snapshot: set[str] = set()
        # Baseline count of ``unusual_activity`` phrase occurrences in the
        # body. The phrase persists in the DOM as a "Failed" card after
        # any prior unusual_activity hit, so a flat substring match
        # would re-fire on every subsequent round. Comparing against
        # this baseline ensures only NEW occurrences (count > baseline)
        # flip the WS into UNUSUAL_ACTIVITY state. Set in ``open()`` and
        # refreshed at the start of each round / after each success.
        self._unusual_phrase_baseline: int = 0
        # Cached compiled regex for candidate src filtering.
        self._candidate_src_re = re.compile(self._cfg.selectors.candidate_src_pattern)

    # --- lifecycle -------------------------------------------------------

    def open(self) -> PageState:
        try:
            from patchright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise FlowPortError(
                "patchright is not installed; install it or run with --mock"
            ) from exc

        if not self.profile_path.exists():
            raise FlowPortError(
                f"profile path does not exist: {self.profile_path} "
                "(run 'flow-harvester login-flow --workstation <id>' first)"
            )

        # Defensive cleanup of orphan Chrome / SingletonLock left by a
        # previous session that died mid-task (server SIGKILL, Chrome
        # crash, customer closing the cmd window). Without this the
        # new launch_persistent_context would either attach to the
        # dead session or fail with TargetClosedError. Customer-side
        # bundled exe has no shell to fix this manually.
        from app.workstations.profile_check import clean_profile_lock
        clean_profile_lock(self.profile_path)

        self._pw = sync_playwright().start()
        try:
            # Patchright recommends minimal customisation: just channel="chrome"
            # in non-headless mode without custom UA / args / init scripts.
            # It handles its own anti-detection (Runtime.enable leak,
            # ExecutionContext isolation, AutomationControlled flag) and
            # piling our own stealth on top creates conflicts that
            # caused ERR_CONNECTION_CLOSED in earlier attempts.
            # Patchright official recommendation: channel="chrome" +
            # headless=False + no_viewport=True + no custom UA / args.
            # ``no_viewport`` lets Chrome use its default window size
            # which is itself a fingerprint signal — fixed 1280x800
            # viewport is suspicious because real users have varied
            # window sizes. Drop ``slow_mo`` and ``accept_downloads``
            # too — accept_downloads stays True via Playwright default.
            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_path),
                channel="chrome",
                headless=self._headless,
                no_viewport=True,
                chromium_sandbox=True,
            )
        except Exception as exc:
            self._teardown_pw()
            raise FlowPortError(f"failed to launch browser: {exc}") from exc

        # Reuse existing tab if present (some profiles open about:blank).
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = self._context.new_page()
        self._page.set_default_timeout(self.page_action_timeout_sec * 1000)

        # Prefer the per-workstation project URL — Flow's generation UI only
        # exists at /fx/tools/flow/project/<id>. Falling back to entry_url
        # only as a sanity check (profile probe / login validation).
        target_url = self._project_url or self.entry_url
        try:
            self._page.goto(target_url, wait_until="domcontentloaded")
        except Exception as exc:
            _LOG.warning("page goto failed: %s", exc)
            return PageState.PAGE_LOAD_FAILED

        # Set the unusual_activity phrase baseline from whatever is in
        # the DOM right after page load. Any "Failed" cards left over
        # from prior sessions are now part of the baseline and will be
        # ignored by the classifier; only NEW occurrences during this
        # session will trigger UNUSUAL_ACTIVITY.
        self._refresh_unusual_phrase_baseline()

        state = self._classify_state()
        if state != PageState.READY:
            return state

        # Dismiss any cookie/TOS/agreement modal *first* — they typically
        # block other interactions (Flow won't let you click Create until
        # you've agreed to the model's TOS, for instance).
        self._dismiss_popups()

        if self._ensure_video_mode:
            try:
                self._select_video_mode()
            except Exception as exc:  # noqa: BLE001 — best-effort
                _LOG.warning("video-mode preflight failed: %s", exc)
        return self._classify_state()

    def _dismiss_popups(self) -> None:
        """Click any open agreement / cookie / TOS dialog button.

        Flow shows a model-specific TOS dialog ("you must accept to use Veo")
        the first time a session uses a paid model in a project. Cookie
        banners may also appear when cookies are reset. Both block
        downstream interactions, so we sweep them up before applying mode
        preset / typing prompts.

        Selectors are config-driven (``popup_dismiss_buttons`` in
        flow-selectors.yaml). For each one we find a visible match and
        click it; if multiple match, we click the first.
        """
        # Flow's "what's new" changelog overlay (an iframe-card top-right)
        # intercepts pointer events for ~30s before auto-dismissing —
        # observed in the wild causing 116 click retries on the prompt
        # editor. It has no close button and is not a role=dialog so
        # the standard popup_dismiss_buttons can't reach it. Strip it
        # from DOM so subsequent clicks are not blocked.
        try:
            removed = self._page.evaluate(
                """
                () => {
                    const sel = 'iframe[src*="aitestkitchen"][src*="changelogs"], '
                              + 'a[href="/fx/tools/flow/changelogs"]';
                    const anchor = document.querySelector(sel);
                    if (!anchor) return null;
                    // Climb up to a likely overlay card (max 6 levels).
                    let p = anchor;
                    for (let i = 0; i < 6; i++) {
                        if (!p.parentElement
                            || p.parentElement === document.body
                            || p.parentElement.tagName === 'MAIN') break;
                        p = p.parentElement;
                    }
                    const desc = p.tagName + '.'
                               + (p.className || '').toString().slice(0, 60);
                    p.remove();
                    return desc;
                }
                """
            )
            if removed:
                _LOG.info("[popup] removed changelog overlay (%s)", removed)
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("[popup] changelog removal skipped: %s", exc)

        selectors = getattr(self._cfg, "popup_dismiss_buttons", None) or []
        if not selectors:
            return
        for sel in selectors:
            try:
                loc = self._page.locator(sel)
                if loc.count() == 0:
                    continue
                first = loc.first
                if not first.is_visible():
                    continue
                txt = ""
                try:
                    txt = (first.inner_text(timeout=1_500) or "").strip()
                except Exception:  # noqa: BLE001
                    pass
                first.click(timeout=3_000)
                _LOG.info("[popup] dismissed via %s (text=%r)", sel, txt[:60])
                self._page.wait_for_timeout(300)
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("[popup] %s skipped: %s", sel, exc)

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("context.close() raised: %s", exc)
        self._teardown_pw()

    def _teardown_pw(self) -> None:
        self._page = None
        self._context = None
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = None

    # --- state classification --------------------------------------------

    def _body_text(self) -> str:
        """Rendered-visible body text for phrase classification.

        Uses Playwright's ``inner_text`` which returns ONLY rendered-
        visible text. Tried switching to ``textContent`` (which
        includes ``display:none`` / hidden / ARIA / tooltip text) to
        avoid one specific false-negative class on stale Failed-card
        baselines, but textContent picked up ``'Flow is experiencing
        high demand'``, ``"don't have access to Flow"`` and similar
        phrases inside Flow's hidden help-center snippets / hover
        tooltips, dragging healthy accounts straight into manual_check
        the moment patchright opened the page. Symmetric inner_text
        usage (both baseline AND current count read the same way) is
        the safer default — false-positive Failed-card detection is
        bounded by the strike system, false-positive whole-page
        takeover detection is not.
        """
        if self._page is None:
            return ""
        try:
            return self._page.locator("body").inner_text(timeout=5_000) or ""
        except Exception:  # noqa: BLE001
            return ""

    _logged_stale_phrase: bool = False

    def _refresh_unusual_phrase_baseline(self) -> None:
        """Snapshot how many ``unusual_activity`` phrase occurrences are
        currently in the DOM. Subsequent classifies treat that count as
        "stale, already there" and only flag if the count grows.
        """
        body = self._body_text()
        new_baseline = _count_phrase_occurrences(
            body, self._cfg.state_phrases.unusual_activity,
        )
        if new_baseline != self._unusual_phrase_baseline:
            _LOG.info(
                "[classify] unusual_activity baseline: %d -> %d",
                self._unusual_phrase_baseline, new_baseline,
            )
        self._unusual_phrase_baseline = new_baseline

    def _classify_state(self) -> PageState:
        body = self._body_text()
        phrases = self._cfg.state_phrases
        # Account-level access denial: full-page takeover, must match
        # Flow's full sentence to avoid help-text false-positives.
        hit = _phrase_match_which(body, phrases.no_flow_access)
        if hit is not None:
            _LOG.warning("[classify] NO_FLOW_ACCESS matched %r", hit)
            return PageState.NO_FLOW_ACCESS
        # Service-level errors next — phrases like "Audio generation failed"
        # are unambiguous, while "unusual activity" can also appear in help
        # text. Order matters here: matches by specificity, not severity.
        hit = _phrase_match_which(body, phrases.service_unavailable)
        if hit is not None:
            _LOG.info("[classify] SERVICE_UNAVAILABLE matched %r", hit)
            return PageState.SERVICE_UNAVAILABLE
        hit = _phrase_match_which(body, phrases.unusual_activity)
        if hit is not None:
            # The phrase also appears in stale "Failed" cards Flow leaves
            # in the project library after a prior unusual_activity hit
            # (each card has a "Retry / Delete / Reuse Prompt" footer
            # plus the phrase verbatim). Counting occurrences and
            # comparing against a baseline taken at round start lets us
            # distinguish "old card still around" from "new ban this
            # round". The baseline is set in ``open()`` and refreshed
            # at the start of ``wait_for_round_complete`` and after a
            # successful round.
            current_count = _count_phrase_occurrences(body, phrases.unusual_activity)
            baseline = getattr(self, "_unusual_phrase_baseline", 0)
            if current_count <= baseline:
                # Stale-card branch: rate-limit the log so a 5-min wait
                # loop doesn't drop ~150 identical info lines. Log once
                # when the state first becomes "stale" and keep silent
                # afterwards until the page state changes.
                if not getattr(self, "_logged_stale_phrase", False):
                    idx = body.lower().find(hit.lower())
                    ctx = body[max(0, idx - 60): idx + len(hit) + 80]
                    _LOG.info(
                        "[classify] unusual_activity phrase %r seen but "
                        "count=%d <= baseline=%d — stale Failed card, "
                        "treating as healthy ; ctx=%r (suppressing further "
                        "identical log lines until state changes)",
                        hit, current_count, baseline, ctx,
                    )
                    self._logged_stale_phrase = True
            else:
                _LOG.warning(
                    "[classify] UNUSUAL_ACTIVITY new occurrence (count=%d, "
                    "baseline=%d) matched %r",
                    current_count, baseline, hit,
                )
                return PageState.UNUSUAL_ACTIVITY
        else:
            # Phrase no longer in body — reset the stale-log latch so the
            # next stale occurrence is logged once.
            self._logged_stale_phrase = False
        hit = _phrase_match_which(body, phrases.captcha_or_verification)
        if hit is not None:
            _LOG.info("[classify] CAPTCHA matched %r", hit)
            return PageState.CAPTCHA_OR_VERIFICATION
        if _phrase_match(body, phrases.login_required):
            # Login phrases tend to also match Flow's signed-out state. Only
            # flag if the prompt textarea is missing, otherwise the user is
            # on Flow and the phrase is a false positive.
            prompt_present = (
                self._page is not None
                and self._page.locator(self._cfg.selectors.prompt_input).count() > 0
            )
            if not prompt_present:
                return PageState.LOGIN_REQUIRED
        if _phrase_match(body, phrases.page_load_failed):
            return PageState.PAGE_LOAD_FAILED
        return PageState.READY

    # --- actions ---------------------------------------------------------

    def upload_source_assets(self, assets: list[SourceAsset]) -> None:
        """Attach one or more source assets to the prompt as inline chips.

        Flow has two distinct prompt-attach UIs depending on which
        sub-tab is active:

        * **Ingredients sub-tab** — single ``+`` button next to the
          prompt opens a picker dialog. All uploads land in one
          unordered slot.
        * **Frames sub-tab** — two named slots, ``Start`` and ``End``,
          flanking a swap-arrow icon. Each opens the same picker
          dialog. Used for first-frame / last-frame video generation.

        Routing rule: if any asset has ``kind`` in
        ``{"first_frame", "last_frame"}`` we use the frames path
        (asserts subtab=frames is active and the named-button
        selectors are configured). Otherwise we use the ingredients
        path (single ``+`` button).

        Per-asset flow (shared by both paths):
            1. Dismiss any open popups.
            2. Click the trigger button (``+`` / ``Start`` / ``End``).
            3. Wait for the picker ``[role="dialog"][data-state="open"]``.
            4. Reuse-from-library if the same filename is already in
               the project asset list, else click "Upload image" →
               OS file chooser → set_files.
            5. Wait for the dialog to close (image now a chip in prompt).

        If the prompt-attach selectors aren't configured (legacy yaml
        or non-Flow targets in tests), the adapter falls back to the
        old ``set_input_files`` path on the hidden file input.
        """
        if not assets:
            raise FlowPortError("upload_source_assets called with empty list")
        self._require_page()

        sel_btn = self._cfg.selectors.prompt_attach_button
        sel_dlg = self._cfg.selectors.prompt_attach_dialog
        sel_target = self._cfg.selectors.prompt_attach_upload_target
        sel_start = self._cfg.selectors.prompt_attach_button_start
        sel_end = self._cfg.selectors.prompt_attach_button_end

        # ── Frames mode? (any asset tagged first_frame / last_frame)
        frame_kinds = {"first_frame", "last_frame"}
        if any(a.kind in frame_kinds for a in assets):
            if not (sel_start and sel_end and sel_dlg and sel_target):
                raise FlowPortError(
                    "frames mode upload requires prompt_attach_button_start "
                    "/ prompt_attach_button_end / prompt_attach_dialog "
                    "/ prompt_attach_upload_target selectors to be configured"
                )
            self._upload_via_frame_buttons(
                assets, sel_start=sel_start, sel_end=sel_end,
                sel_dlg=sel_dlg, sel_target=sel_target,
            )
            return

        # ── Ingredients mode (default).
        if sel_btn and sel_dlg and sel_target:
            self._upload_via_prompt_attach(
                assets, sel_btn=sel_btn, sel_dlg=sel_dlg, sel_target=sel_target
            )
            return

        # ── Legacy fallback: hidden file input on the Ingredients sub-tab.
        self._upload_via_hidden_input(assets)

    def _upload_via_frame_buttons(
        self,
        assets: list[SourceAsset],
        *,
        sel_start: str,
        sel_end: str,
        sel_dlg: str,
        sel_target: str,
    ) -> None:
        """Route assets to Start / End slots by ``kind`` and upload each
        through the same dialog flow as ingredients.

        Refuses on ambiguity (more than one first_frame / last_frame, or
        any other kind mixed in) — frames mode expects exactly one
        first_frame and/or one last_frame asset.
        """
        first: SourceAsset | None = None
        last: SourceAsset | None = None
        for a in assets:
            if a.kind == "first_frame":
                if first is not None:
                    raise FlowPortError(
                        "frames mode expects exactly one first_frame asset"
                    )
                first = a
            elif a.kind == "last_frame":
                if last is not None:
                    raise FlowPortError(
                        "frames mode expects exactly one last_frame asset"
                    )
                last = a
            else:
                raise FlowPortError(
                    f"frames mode received asset with unsupported kind={a.kind!r} "
                    "(expected first_frame or last_frame)"
                )
        if first is None and last is None:
            raise FlowPortError(
                "frames mode requires at least one of first_frame / last_frame"
            )

        sequence: list[tuple[str, str, SourceAsset]] = []
        if first is not None:
            sequence.append((sel_start, "Start", first))
        if last is not None:
            sequence.append((sel_end, "End", last))

        for idx, (button_sel, label, asset) in enumerate(sequence, start=1):
            self._upload_one_asset(
                asset,
                idx=idx, total=len(sequence),
                button_sel=button_sel, button_label=label,
                sel_dlg=sel_dlg, sel_target=sel_target,
                settle_after=(idx < len(sequence)),
            )

    def _upload_via_prompt_attach(
        self,
        assets: list[SourceAsset],
        *,
        sel_btn: str,
        sel_dlg: str,
        sel_target: str,
    ) -> None:
        for idx, asset in enumerate(assets, start=1):
            self._upload_one_asset(
                asset,
                idx=idx, total=len(assets),
                button_sel=sel_btn, button_label="+",
                sel_dlg=sel_dlg, sel_target=sel_target,
                settle_after=(idx < len(assets)),
            )

    def _upload_one_asset(
        self,
        asset: SourceAsset,
        *,
        idx: int,
        total: int,
        button_sel: str,
        button_label: str,
        sel_dlg: str,
        sel_target: str,
        settle_after: bool,
    ) -> None:
        """Upload a single asset through the click-button → dialog →
        upload-image flow.

        Shared by ingredients (``+`` trigger) and frames (``Start`` /
        ``End`` triggers). Both UIs open the same picker dialog; only
        the trigger selector + label differ. ``button_label`` is a
        human-readable token used in error / log messages.
        """
        self._dismiss_popups()

        # 1. Click the trigger to open the picker.
        try:
            trigger = self._page.locator(button_sel).first
            trigger.wait_for(
                state="attached",
                timeout=self.page_action_timeout_sec * 1000,
            )
            trigger.click()
        except Exception as exc:
            raise FlowPortError(
                f"failed to click prompt-attach {button_label!r} for "
                f"asset #{idx} ({asset.path}): {exc}"
            ) from exc

        # 2. Wait for the picker dialog.
        try:
            dialog = self._page.locator(sel_dlg).first
            dialog.wait_for(state="visible", timeout=10_000)
        except Exception as exc:
            raise FlowPortError(
                f"prompt-attach dialog didn't open after clicking "
                f"{button_label!r} for asset #{idx}: {exc}"
            ) from exc

        # 3. Reuse-existing-or-upload: clicking the dialog's
        # "Upload image" path in Flow always pushes a *new* row into
        # the project asset library (even if the same filename already
        # exists), causing the library to fill up with duplicates over
        # many runs. Instead, look for a thumbnail whose ``<img alt>``
        # matches our filename and click it.
        #
        # Success signal is **dialog auto-closure** — Flow dismisses
        # the picker the moment a thumbnail is successfully selected.
        # If reuse fails, the dialog stays open and we fall straight
        # through to Upload image (no need to close + re-open).
        existing = self._find_existing_library_asset(dialog, asset.path.name)
        attached_via_reuse = False
        if existing is not None:
            attached_via_reuse = self._try_attach_existing(
                dialog, existing, asset.path.name
            )
            if attached_via_reuse:
                _LOG.info(
                    "uploaded asset %d/%d via %s (reused library) kind=%s name=%s",
                    idx, total, button_label, asset.kind, asset.path.name,
                )
            else:
                _LOG.warning(
                    "[upload] reuse click failed for %s (no strategy closed dialog) "
                    "— falling back to fresh upload (dialog still open)",
                    asset.path.name,
                )

        if not attached_via_reuse:
            try:
                upload_target = dialog.locator(sel_target).first
                with self._page.expect_file_chooser(timeout=15_000) as fc_info:
                    upload_target.click()
                fc_info.value.set_files(str(asset.path))
            except Exception as exc:
                try:
                    self._page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
                raise FlowPortError(
                    f"prompt-attach upload failed via {button_label!r} for "
                    f"asset #{idx} ({asset.path}): {exc}"
                ) from exc
            _LOG.info(
                "uploaded asset %d/%d via %s (fresh upload) kind=%s path=%s",
                idx, total, button_label, asset.kind, asset.path,
            )

        # 4. Wait for the dialog to close (image is now a prompt chip).
        try:
            dialog.wait_for(state="hidden", timeout=10_000)
        except Exception:  # noqa: BLE001
            try:
                self._page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            self._page.wait_for_timeout(300)

        # 5. Settle window so the chip mounts before the caller opens
        # the next picker iteration; otherwise Flow can drop the
        # in-flight chip.
        if settle_after:
            self._page.wait_for_timeout(800)

    def _try_attach_existing(self, dialog, existing, filename: str) -> bool:
        """Click strategies for attaching an existing library asset.

        Flow's React handler may be bound to the inner ``<img>``, an
        intermediate wrapper, or only respond to keyboard activation on
        the ``tabindex=0`` wrapper. We try several click shapes from
        most- to least-specific, plus an Enter-key fallback.

        **Success signal: dialog auto-closes.** Flow dismisses the
        picker dialog the moment a thumbnail is successfully selected;
        a click that misses the React handler leaves the dialog open.
        We can't rely on counting ``<img>`` chips inside the prompt
        because Slate may render void elements without an actual
        ``<img>`` tag (e.g. CSS background, SVG, etc.).
        """
        img_locator = dialog.locator(f'img[alt="{filename}"]').first

        attempts = [
            ("img-direct", lambda: img_locator.click(timeout=3_000)),
            ("wrapper", lambda: existing.click(timeout=3_000)),
            ("force-wrapper", lambda: existing.click(timeout=3_000, force=True)),
            ("focus-enter", lambda: (existing.focus(), self._page.keyboard.press("Enter"))),
            ("evaluate-click", lambda: existing.evaluate("el => el.click()")),
            ("img-double", lambda: img_locator.dblclick(timeout=3_000)),
        ]

        for name, action in attempts:
            try:
                action()
            except Exception as exc:  # noqa: BLE001
                _LOG.info("[reuse/click] strategy %s raised: %s", name, exc)
                # Move to next strategy without trying to verify.
                continue

            # Primary success signal: Flow auto-dismisses the picker
            # dialog when an asset is selected. Wait up to 3s.
            try:
                dialog.wait_for(state="hidden", timeout=3_000)
                _LOG.info("[reuse/click] strategy %s closed dialog (asset attached)", name)
                return True
            except Exception:  # noqa: BLE001
                _LOG.info(
                    "[reuse/click] strategy %s did not close dialog within 3s; trying next",
                    name,
                )
                # If dialog still open, our click missed the handler —
                # try next strategy without re-opening.
                continue
        return False

    def _find_existing_library_asset(self, dialog, filename: str):
        """Locate a clickable thumbnail in the picker dialog whose underlying
        ``<img>`` has the given filename as its ``alt`` attribute. Returns the
        locator (positioned on the first visible match) or None.

        Library thumbnails load asynchronously after the picker dialog
        mounts, so we **wait** up to 5 seconds for the specific filename's
        ``<img>`` to attach before declaring "not in library". Without
        this wait, every round in a multi-round task races the library
        fetch and falls back to fresh upload — accumulating duplicate
        library rows.
        """
        try:
            dialog.locator(f'img[alt="{filename}"]').first.wait_for(
                state="attached", timeout=5_000
            )
        except Exception:
            # Filename not in library after 5s — log what IS there so we
            # can spot quoting / encoding mismatches at a glance.
            try:
                visible = self._page.evaluate(
                    """
                    (sel) => {
                        const dlg = document.querySelector(sel);
                        if (!dlg) return [];
                        return [...dlg.querySelectorAll('img[alt]')]
                            .slice(0, 30)
                            .map(i => i.getAttribute('alt'));
                    }
                    """,
                    self._cfg.selectors.prompt_attach_dialog,
                ) or []
                _LOG.info(
                    "[upload/reuse] %r not in library; visible alts: %s",
                    filename, visible,
                )
            except Exception:  # noqa: BLE001
                pass
            return None

        candidates = [
            # The clickable wrapper Flow renders for keyboard navigation.
            f'[tabindex="0"]:has(img[alt="{filename}"])',
            f'[role="button"]:has(img[alt="{filename}"])',
            f'button:has(img[alt="{filename}"])',
            # Last-resort: click the img directly. Browsers bubble the click.
            f'img[alt="{filename}"]',
        ]
        for sel in candidates:
            try:
                loc = dialog.locator(sel)
                cnt = loc.count()
                if cnt == 0:
                    continue
                for i in range(min(cnt, 5)):
                    cand = loc.nth(i)
                    try:
                        if cand.is_visible():
                            return cand
                    except Exception:  # noqa: BLE001
                        continue
            except Exception:  # noqa: BLE001
                continue
        return None

    def _upload_via_hidden_input(self, assets: list[SourceAsset]) -> None:
        """Legacy upload path — kept for the local-HTML test mock and any
        flow-selectors.yaml that doesn't yet configure prompt-attach."""
        self._dismiss_popups()
        sel = self._cfg.selectors.upload_button
        element = self._page.locator(sel).first
        try:
            element.wait_for(
                state="attached",
                timeout=self.page_action_timeout_sec * 1000,
            )
        except Exception:
            try:
                cnt = self._page.locator(sel).count()
            except Exception:  # noqa: BLE001
                cnt = -1
            url = self._page.url or "<unknown>"
            raise FlowPortError(
                f"upload selector not found: {sel} (count={cnt}, url={url})"
            )
        for idx, asset in enumerate(assets, start=1):
            self._dismiss_popups()
            try:
                tag = (element.evaluate("el => el.tagName") or "").lower()
                if tag == "input":
                    element.set_input_files(str(asset.path))
                else:
                    with self._page.expect_file_chooser() as fc_info:
                        element.click()
                    fc_info.value.set_files(str(asset.path))
                _LOG.info(
                    "uploaded asset %d/%d (legacy) kind=%s path=%s",
                    idx, len(assets), asset.kind, asset.path,
                )
                if idx < len(assets):
                    self._page.wait_for_timeout(700)
            except FlowPortError:
                raise
            except Exception as exc:
                raise FlowPortError(
                    f"upload failed for asset #{idx} ({asset.path}): {exc}"
                ) from exc

    def paste_prompt(self, prompt: str) -> None:
        """Type a prompt into Flow's Slate.js (contenteditable) editor.

        Slate ignores ``element.fill()`` and synthetic ``input`` events. The
        only reliable path is: focus → select-all → backspace → type via
        keyboard so Slate sees real key events.

        Per-key delay is set so a 200-300 char Chinese / Malay prompt
        takes ~20-40s to type instead of <1s. Veo's behavioral
        fingerprint flags 1000+ chars-per-second typing as automation;
        a humanized cadence keeps the per-generation 'unusual activity'
        flag from firing on accounts that work fine when driven by a
        human at the same workstation.
        """
        import random
        self._require_page()
        # Notice dialog often appears between upload and paste — without
        # this dismiss, the dialog overlay intercepts our click on the
        # editor and the locator times out at 60s.
        self._dismiss_popups()
        sel = self._cfg.selectors.prompt_input
        try:
            locator = self._page.locator(sel).first
            locator.click()
            modifier = "Meta" if self._is_mac() else "Control"
            self._page.keyboard.press(f"{modifier}+A")
            self._page.keyboard.press("Backspace")
            # ``keyboard.type`` per-char is the only reliable path —
            # ``insert_text`` causes Veo to reject the generation with
            # "Failed, oops something went wrong" because Slate's input
            # validation expects keystroke events.
            #
            # Tiny per-char jitter around 80ms keeps the timing
            # distribution human-shaped (real typists have variance,
            # bots default to constant cadence). For a 300-char prompt
            # that's ~24-30s — slightly slower than a fast typist but
            # well within human range.
            jitter_delay = random.randint(60, 110)
            self._page.keyboard.type(prompt, delay=jitter_delay)
        except Exception as exc:
            raise FlowPortError(f"prompt input failed: {exc}") from exc

    def _snapshot_prompt_state(self) -> dict:
        """Return a slim dict describing the Slate editor + its 3 ancestors.

        Flow renders the image chip in a sibling of the Slate editor, not
        inside it, so we count ``<img>`` at the editor itself and at three
        levels of ancestor. Used by ``trigger_generation`` to log a sanity
        check before clicking Create.
        """
        try:
            sel = self._cfg.selectors.prompt_input
            return self._page.evaluate(
                """sel => {
                    const el = document.querySelector(sel);
                    if (!el) return {found: false};
                    const describe = (n) => n ? {
                        text_len: (n.innerText || '').trim().length,
                        text_preview: (n.innerText || '').trim().slice(0, 60),
                        img_count: n.querySelectorAll('img').length,
                    } : null;
                    return {
                        found: true,
                        editor: describe(el),
                        parent_1: describe(el.parentElement),
                        parent_2: describe(el.parentElement && el.parentElement.parentElement),
                        parent_3: describe(el.parentElement && el.parentElement.parentElement
                                            && el.parentElement.parentElement.parentElement),
                    };
                }""",
                sel,
            ) or {}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    def _is_mac(self) -> bool:
        try:
            ua = self._page.evaluate("() => navigator.platform") or ""
            return "Mac" in ua
        except Exception:  # noqa: BLE001
            return False

    def _select_video_mode(self) -> None:
        """Force Flow's project UI into the configured ``flow_mode`` state.

        The order matters: top tab → sub-tab → aspect → output count →
        duration → model. Each step waits briefly for the SPA to re-render
        before moving on, because Flow tears down panels when you switch
        the top tab and the next selector won't exist for a few frames.
        """
        spec = self._flow_mode_spec
        if spec is None:
            return
        mc = self._cfg.mode_controls
        _LOG.info("applying flow_mode preset: %s", _spec_summary(spec))

        # Flow keeps the option-panel collapsed by default — tab buttons
        # aren't in DOM until the user clicks the model+settings summary
        # button. Open it first.
        # Failing here used to silently fall through and let the task
        # generate with the project's *default* model (often Nano Banana
        # 2 → PNG output). That looks like success to the scheduler but
        # produces unusable artifacts. Raise instead so the round fails
        # loudly and operations notices.
        if not self._open_settings_panel(mc):
            raise FlowPortError(
                "mode preset: settings_trigger not found — refine "
                "config/flow-selectors.yaml :: mode_controls.settings_trigger "
                "(diagnostic enumeration above lists candidate buttons)"
            )

        try:
            self._page.locator('button[role="tab"]').first.wait_for(
                state="attached", timeout=4_000
            )
        except Exception as exc:
            raise FlowPortError(
                "mode preset: no [role=tab] buttons after settings panel "
                "opened — Flow UI may have changed"
            ) from exc

        # 1. Top-level tab: video / image
        tab = getattr(spec, "tab", None)
        if tab == "video" and mc.tab_video:
            self._click_inactive_tab(mc.tab_video, settle_ms=400, label="tab_video")
        elif tab == "image" and mc.tab_image:
            self._click_inactive_tab(mc.tab_image, settle_ms=400, label="tab_image")

        # 2. Sub-tab: ingredients / frames (only when in video mode)
        subtab = getattr(spec, "subtab", None)
        if subtab == "ingredients" and mc.subtab_ingredients:
            self._click_inactive_tab(mc.subtab_ingredients, settle_ms=200,
                                      label="subtab_ingredients")
        elif subtab == "frames" and mc.subtab_frames:
            self._click_inactive_tab(mc.subtab_frames, settle_ms=200,
                                      label="subtab_frames")

        # 3. Aspect ratio
        aspect = getattr(spec, "aspect", None)
        aspect_sel = {
            "9:16": mc.aspect_9_16,
            "16:9": mc.aspect_16_9,
            "1:1": mc.aspect_1_1,
        }.get(aspect)
        if aspect_sel:
            self._click_inactive_tab(aspect_sel, settle_ms=100, label=f"aspect_{aspect}")

        # 4. Output count (x1..x4)
        output_count = getattr(spec, "output_count", None)
        if output_count and mc.output_count_template:
            sel = mc.output_count_template.replace("{N}", str(output_count))
            self._click_inactive_tab(sel, settle_ms=100, label=f"output_x{output_count}")

        # 5. Duration (4s / 6s / 8s)
        duration = getattr(spec, "duration_sec", None)
        if duration and mc.duration_template:
            sel = mc.duration_template.replace("{S}", str(duration))
            self._click_inactive_tab(sel, settle_ms=100, label=f"duration_{duration}s")

        # 6. Model dropdown
        model = getattr(spec, "model", None)
        if model and mc.model_dropdown_button and mc.model_menu_item_template:
            try:
                self._set_model(model, mc)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("[mode] model selection skipped: %s", exc)

        # Close any popovers/menus left open by the preset application —
        # without this, the still-open settings panel covers the prompt
        # input and the next paste_prompt() click silently fails.
        self._close_open_popovers(mc)

    def _open_settings_panel(self, mc) -> bool:
        """Click the model+settings summary button so the option panel mounts.

        Tries each selector in ``mc.settings_trigger`` in order. If the
        button is already ``data-state='open'``, returns True without
        clicking. Returns False only if no candidate trigger was found —
        the caller then raises rather than silently producing wrong-type
        output.
        """
        triggers = mc.settings_trigger or []
        if not triggers:
            return False

        # Fresh projects show "Loading..." for ~10s and the settings
        # summary button is one of the *last* DOM elements to mount —
        # after the prompt input, after the more_vert/Add Media chrome,
        # etc. Naively waiting for ``button[data-state]`` returns
        # immediately on the chrome buttons, then ``has-text(crop_)``
        # races and reports zero hits. Instead, wait for any *configured*
        # trigger to actually attach. ``locator(", ".join(...))`` builds
        # one Playwright locator that matches any of the candidates.
        combined = ", ".join(triggers)
        try:
            self._page.locator(combined).first.wait_for(
                state="attached", timeout=20_000
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "[mode] no settings_trigger candidate mounted within 20s: %s", exc
            )

        for sel in triggers:
            try:
                loc = self._page.locator(sel)
                if loc.count() == 0:
                    continue
                btn = loc.first
                state = (btn.get_attribute("data-state") or "").lower()
                if state == "open":
                    _LOG.info("[mode] settings panel already open (%s)", sel)
                    return True
                btn.click()
                self._page.wait_for_timeout(500)
                _LOG.info("[mode] opened settings panel via %s (was state=%s)",
                          sel, state or "<unset>")
                return True
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("[mode] settings_trigger candidate %s skipped: %s", sel, exc)

        # None of the configured selectors matched. Dump every button with
        # a data-state attribute so the operator can read out the real text
        # and refine ``settings_trigger``. Done via page.evaluate so we
        # don't pay 13 round-trips per call (and so empty-text buttons
        # show up instead of being silently swallowed by inner_text timeout).
        try:
            info = self._page.evaluate(
                """
                () => [...document.querySelectorAll('button[data-state]')]
                    .slice(0, 30)
                    .map(b => ({
                        state: b.getAttribute('data-state'),
                        visible: !!(b.offsetWidth || b.offsetHeight),
                        text: ((b.innerText || b.textContent || '')
                                  .trim()
                                  .replace(/\\n/g, ' | ')
                                  .slice(0, 160)),
                        aria: b.getAttribute('aria-label') || null,
                    }))
                """
            ) or []
            _LOG.warning(
                "[mode/diag] settings_trigger missed; %d candidates:",
                len(info),
            )
            for i, d in enumerate(info):
                _LOG.warning(
                    "[mode/diag]   #%d state=%s visible=%s aria=%r text=%r",
                    i, d.get("state"), d.get("visible"),
                    d.get("aria"), d.get("text"),
                )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("[mode/diag] enumeration failed: %s", exc)
        return False

    def _click_inactive_tab(
        self,
        selector: str,
        *,
        settle_ms: int = 0,
        label: str = "tab",
        attach_timeout_ms: int = 3_000,
    ) -> None:
        try:
            loc = self._page.locator(selector).first
            try:
                loc.wait_for(state="attached", timeout=attach_timeout_ms)
            except Exception:
                _LOG.warning("[mode/%s] selector not in DOM after %dms: %s",
                             label, attach_timeout_ms, selector)
                return
            state = (loc.get_attribute("data-state") or "").lower()
            if state == "active":
                _LOG.info("[mode/%s] already active, skipping", label)
                return
            loc.click()
            _LOG.info("[mode/%s] clicked (was state=%s)", label, state or "<unset>")
            if settle_ms:
                self._page.wait_for_timeout(settle_ms)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("[mode/%s] click failed on %s: %s", label, selector, exc)

    def _close_open_popovers(self, mc) -> None:
        """Make sure no radix popover / dropdown is still ``data-state=open``.

        Strategy:
          1. Press Escape twice — closes the model menu and the settings
             panel if they're stacked.
          2. If the settings panel is still ``open``, re-click its trigger
             to toggle it shut (radix toggles state on the same button).
          3. As a final dispenser, click ``<body>`` to take focus away.
        """
        try:
            self._page.keyboard.press("Escape")
            self._page.wait_for_timeout(150)
            self._page.keyboard.press("Escape")
            self._page.wait_for_timeout(150)
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("[mode] escape press failed: %s", exc)

        triggers = mc.settings_trigger or []
        for sel in triggers:
            try:
                loc = self._page.locator(sel)
                if loc.count() == 0:
                    continue
                btn = loc.first
                state = (btn.get_attribute("data-state") or "").lower()
                if state != "open":
                    continue
                btn.click()
                self._page.wait_for_timeout(200)
                _LOG.info("[mode] toggled settings panel closed via %s", sel)
                break
            except Exception:  # noqa: BLE001
                continue

        # Belt-and-braces: drop focus on body so any lingering tooltip
        # or hover state releases. Failures here are silent — the click
        # is purely defensive.
        try:
            self._page.locator("body").click(position={"x": 5, "y": 5}, timeout=1_500)
        except Exception:  # noqa: BLE001
            pass

    def _set_model(self, target_name: str, mc) -> None:
        """Open the model dropdown and click the menu item matching ``target_name``.

        Strategy:
          1. Find the model picker — a button containing ``arrow_drop_down``
             whose text also matches one of the configured model keywords.
          2. If the picker text already contains ``target_name``, do nothing.
          3. Click the picker, wait for the menu, click the menu item whose
             text contains ``target_name``, wait for the menu to close.

        Menu items are tried with multiple selector patterns because
        radix can render them as ``[role="menuitem"]``, ``[role="option"]``,
        plain ``button``, or any ``[data-radix-collection-item]`` depending
        on which radix primitive Flow uses.
        """
        picker = self._find_model_picker(mc)
        if picker is None:
            _LOG.warning("[mode/model] picker not found in DOM")
            return
        try:
            current_text = (picker.inner_text(timeout=3_000) or "").replace("\n", " | ")
        except Exception:  # noqa: BLE001
            current_text = ""
        if target_name in current_text:
            _LOG.info("[mode/model] already %r (text=%r); skipping picker",
                      target_name, current_text[:80])
            return

        _LOG.info("[mode/model] current=%r target=%r — opening dropdown",
                  current_text[:80], target_name)
        picker.click()
        self._page.wait_for_timeout(500)  # animate in

        # Try multiple selector patterns so we cover whichever radix
        # primitive Flow uses for the menu (DropdownMenu / Select / etc.).
        configured = (mc.model_menu_item_template or "").replace("{NAME}", target_name)
        candidates = [
            configured,
            f'[role="menuitem"]:has-text("{target_name}")',
            f'[role="option"]:has-text("{target_name}")',
            f'[data-radix-collection-item]:has-text("{target_name}")',
            f'button:has-text("{target_name}")',
            f'div[data-state]:has-text("{target_name}")',
        ]
        for sel in candidates:
            if not sel:
                continue
            try:
                loc = self._page.locator(sel)
                if loc.count() == 0:
                    continue
                # Pick the first VISIBLE match (radix sometimes leaves
                # collapsed copies in DOM).
                for i in range(min(loc.count(), 5)):
                    cand = loc.nth(i)
                    try:
                        if not cand.is_visible():
                            continue
                    except Exception:  # noqa: BLE001
                        continue
                    cand.click(timeout=3_000)
                    _LOG.info("[mode/model] clicked menu item via %s", sel)
                    self._page.wait_for_timeout(400)
                    return
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("[mode/model] candidate %s skipped: %s", sel, exc)

        # Nothing matched — dump visible menu items so we can refine.
        try:
            info = self._page.evaluate(
                """
                () => [...document.querySelectorAll(
                    '[role="menuitem"], [role="option"], '
                    + '[data-radix-collection-item], '
                    + 'div[data-state] [role="presentation"], '
                    + 'div[data-state]:not([data-state="closed"]) > * > *'
                )]
                  .filter(e => (e.offsetWidth || e.offsetHeight))
                  .slice(0, 40)
                  .map(e => ({
                      role: e.getAttribute('role'),
                      tag: e.tagName.toLowerCase(),
                      text: ((e.innerText || '').trim()
                                .replace(/\\n/g, ' | ').slice(0, 140)),
                  }))
                """
            ) or []
            _LOG.warning(
                "[mode/model/diag] no menu item matched %r; %d visible candidates:",
                target_name, len(info),
            )
            for i, d in enumerate(info):
                _LOG.warning(
                    "[mode/model/diag]   #%d role=%s tag=%s text=%r",
                    i, d.get("role"), d.get("tag"), d.get("text"),
                )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("[mode/model/diag] enumerate failed: %s", exc)
        # Close the (still-open) menu before bubbling up.
        self._page.keyboard.press("Escape")
        raise FlowPortError(
            f"could not find model menu item matching {target_name!r}"
        )

    def _find_model_picker(self, mc):
        if not mc.model_dropdown_button:
            return None
        loc = self._page.locator(mc.model_dropdown_button)
        try:
            count = loc.count()
        except Exception:  # noqa: BLE001
            return None
        for i in range(min(count, 12)):
            try:
                cand = loc.nth(i)
                txt = cand.inner_text(timeout=1_500) or ""
            except Exception:  # noqa: BLE001
                continue
            if any(kw in txt for kw in mc.model_keywords):
                return cand
        return None

    def trigger_generation(self) -> None:
        """Snapshot existing candidates, then click Create.

        The snapshot lets ``wait_for_round_complete`` diff out the new
        candidates produced by *this* round (the gallery contains every
        video and image the user has ever generated in this project).

        Flow shows a one-time "Notice / I agree" dialog after the first
        Create click in a session — we sweep for it twice (immediately and
        after a short wait) before returning so ``wait_for_round_complete``
        doesn't deadlock waiting for candidates that will never appear.

        Adds a short randomized "review pause" before clicking Create.
        A human always pauses after typing to re-read the prompt
        before submitting; clicking Create within milliseconds of the
        last keystroke is one of the cleanest bot signatures, and Flow
        has been observed to flag generations with that pattern as
        ``unusual activity`` even when the account is otherwise
        reachable manually.
        """
        import random
        self._require_page()
        # Defensive sweep: same reason as paste_prompt — overlays kill
        # our click otherwise.
        self._dismiss_popups()
        # Human-like review pause between typing and clicking Create.
        # 1.5-3.5s is wide enough to not look mechanical, narrow enough
        # to not slow throughput meaningfully (round time is dominated
        # by Veo's 60-90s encode anyway).
        self._page.wait_for_timeout(random.randint(1500, 3500))
        try:
            self._candidate_snapshot = set(self._collect_candidates_full().keys())
            _LOG.debug("pre-trigger snapshot has %d candidates", len(self._candidate_snapshot))
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("snapshot before trigger failed: %s", exc)
            self._candidate_snapshot = set()
        # Verify the prompt is non-empty and has the chip(s) attached
        # before clicking Create. A silent empty prompt makes Flow
        # accept the click but never start a generation, which from
        # outside looks identical to a stuck round.
        state = self._snapshot_prompt_state()
        if state.get("found"):
            ed = state.get("editor") or {}
            chips_total = sum(
                (state.get(scope) or {}).get("img_count", 0)
                for scope in ("editor", "parent_1", "parent_2", "parent_3")
            )
            _LOG.info(
                "[trigger] pre-create: editor.text_len=%d chips_total=%d "
                "(editor=%d, p1=%d, p2=%d, p3=%d) preview=%r",
                ed.get("text_len", 0), chips_total,
                (state.get("editor") or {}).get("img_count", 0),
                (state.get("parent_1") or {}).get("img_count", 0),
                (state.get("parent_2") or {}).get("img_count", 0),
                (state.get("parent_3") or {}).get("img_count", 0),
                ed.get("text_preview", ""),
            )
        sel = self._cfg.selectors.generate_button
        try:
            btn = self._page.locator(sel).first
            # Move the mouse to the button via several intermediate
            # steps before clicking. Default ``.click()`` does an
            # instant move + click, which is bot-shaped — real users
            # generate a continuous mousemove stream as their cursor
            # crosses the page. Patchright's stealth handles flag-
            # level signals; behavior-level signals like this we have
            # to fake ourselves.
            try:
                box = btn.bounding_box()
                if box is not None:
                    target_x = box["x"] + box["width"] / 2
                    target_y = box["y"] + box["height"] / 2
                    self._page.mouse.move(target_x, target_y, steps=15)
            except Exception:  # noqa: BLE001 — best-effort
                pass
            btn.click(delay=random.randint(40, 120))
        except Exception as exc:
            raise FlowPortError(f"generate click failed: {exc}") from exc

        # The "Notice / I agree" dialog typically appears within ~500ms of
        # clicking Create on the first round. Sweep twice so we catch it
        # whether it animates in fast or slow.
        self._dismiss_popups()
        try:
            self._page.wait_for_timeout(800)
        except Exception:  # noqa: BLE001
            pass
        self._dismiss_popups()

    def wait_for_round_complete(self, timeout_sec: int) -> GenerationRoundResult:
        """Wait until new candidates appear and the count is stable.

        Stability check (``stability_window_sec``) prevents us from
        returning early when x4 outputs are still streaming in one-by-one.
        """
        self._require_page()
        deadline = time.time() + max(1, timeout_sec)
        round_start = time.time()
        # Lock in the current ``unusual_activity`` phrase count as the
        # baseline before this round starts. Stale "Failed" cards from
        # earlier rounds count toward the baseline, so only a NEW
        # failure during this wait will exceed it and flip the state
        # to UNUSUAL_ACTIVITY.
        self._refresh_unusual_phrase_baseline()
        # Time-based fallback for "silent" unusual_activity bans —
        # observed in the wild where Flow recycles the existing failed
        # card slot on a new failure (so phrase count stays at baseline)
        # while visibly displaying the ban message. After this many
        # seconds without ANY new candidate appearing, if the phrase is
        # still in the body, escalate to UNUSUAL_ACTIVITY anyway. Set
        # comfortably above Veo Fast's typical 60-90s encode window so
        # genuine slow encodes aren't misclassified as bans.
        no_progress_ban_threshold_sec = 120.0
        # Stability window is the minimum time the new-candidate set must
        # not change before we accept it. Long enough (default 30s) that
        # Veo's poster-image -> mp4-video swap has time to land; short
        # enough that fast image-only generations don't burn extra wall
        # time. Configurable via ``candidate_stability_window_sec``.
        stability_window_sec = self._candidate_stability_window_sec
        last_new: list[tuple[str, str]] = []  # [(src, kind), ...]
        last_change_time = 0.0
        # Sweep popups on the first 5 iterations of the wait loop — Flow
        # sometimes mounts the "Notice / I agree" dialog up to several
        # seconds *after* clicking Create, so a one-shot dismiss right
        # after the click can miss it.
        popup_sweeps_remaining = 5
        # In-round retry budget for Veo audio sub-model failures. These
        # are per-generation flukes (Flow says "you have not been charged
        # for this generation") — we re-click Create up to N times within
        # the same round before giving up. Workstation health is *not*
        # touched.
        retry_budget = 3
        last_retry_phrase_at = 0.0

        while time.time() < deadline:
            if popup_sweeps_remaining > 0:
                self._dismiss_popups()
                popup_sweeps_remaining -= 1

            # Probe for transient generation failures *before* the normal
            # circuit-breaker classifier so they don't get conflated with
            # real account/service issues.
            if retry_budget > 0:
                body = self._body_text()
                phrases = self._cfg.state_phrases
                if _phrase_match(body, phrases.generation_retryable) \
                        and (time.time() - last_retry_phrase_at) > 5.0:
                    last_retry_phrase_at = time.time()
                    retry_budget -= 1
                    _LOG.warning(
                        "[round/retry] generation-retryable phrase detected "
                        "(remaining budget=%d) — re-clicking Create",
                        retry_budget,
                    )
                    try:
                        # Re-trigger generation in the same round. Don't
                        # re-upload assets — the prompt + chips are
                        # still attached.
                        self._page.locator(
                            self._cfg.selectors.generate_button
                        ).first.click(timeout=5_000)
                        # Reset stability tracking — the failed candidates
                        # (if any) shouldn't count against the new attempt.
                        last_new = []
                        last_change_time = 0.0
                    except Exception as exc:  # noqa: BLE001
                        _LOG.warning(
                            "[round/retry] re-click Create failed: %s", exc
                        )
                    time.sleep(2.0)
                    continue

            state = self._classify_state()
            if state in (
                PageState.UNUSUAL_ACTIVITY,
                PageState.LOGIN_REQUIRED,
                PageState.CAPTCHA_OR_VERIFICATION,
                PageState.SERVICE_UNAVAILABLE,
                PageState.PAGE_LOAD_FAILED,
            ):
                return GenerationRoundResult(state=state, candidates=[],
                                              error_message=state.value)

            try:
                current_full = self._collect_candidates_full()
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("candidate probe error: %s", exc)
                time.sleep(2.0)
                continue

            # Time-based ban fallback: if Veo's typical encode window has
            # elapsed and we still haven't seen ANY new candidate, the
            # generation almost certainly failed silently. If the
            # unusual_activity phrase is also present, treat as ban —
            # this catches the case where Flow recycles the failed card
            # slot so phrase count stays equal to baseline.
            elapsed = time.time() - round_start
            new_so_far = [
                (src, kind)
                for src, kind in current_full.items()
                if src not in self._candidate_snapshot
            ]
            if not new_so_far and elapsed > no_progress_ban_threshold_sec:
                body = self._body_text()
                hit = _phrase_match_which(
                    body, self._cfg.state_phrases.unusual_activity
                )
                if hit is not None:
                    _LOG.warning(
                        "[round] no new candidate after %.0fs and "
                        "unusual_activity phrase %r still present — "
                        "treating as ban (count-baseline check missed it; "
                        "Flow likely recycled the Failed card slot)",
                        elapsed, hit,
                    )
                    return GenerationRoundResult(
                        state=PageState.UNUSUAL_ACTIVITY, candidates=[],
                        error_message="unusual_activity (no-progress fallback)",
                    )

            # Stable comparison key includes (src, kind) so an
            # image -> video upgrade for the same UUID counts as a
            # change and resets the stability timer.
            new_only = [
                (src, kind)
                for src, kind in current_full.items()
                if src not in self._candidate_snapshot
            ]

            # ── Early exit: if every new candidate has already been
            # upgraded to ``<video>``, Flow has finished encoding —
            # return immediately, no need to wait the stability window.
            # This is the common Veo path: poster image at ~10s, video
            # mounts at ~60-90s, we return the moment the upgrade is
            # observed without any extra delay.
            if new_only and all(kind == "video" for _, kind in new_only):
                candidates = [
                    CandidateMeta(sequence_no=i + 1, download_handle=src, media_kind=kind)
                    for i, (src, kind) in enumerate(new_only)
                ]
                _LOG.info(
                    "round produced %d new candidate(s) (kinds={'video'}, early-exit)",
                    len(candidates),
                )
                return GenerationRoundResult(state=PageState.READY, candidates=candidates)

            if new_only and new_only == last_new:
                # Set hasn't changed — once stability window passes we
                # accept whatever we have (Nano Banana / Imagen image
                # outputs that will never become videos).
                #
                # Exception: if the configured mode is video, an image
                # candidate is almost certainly Veo's poster placeholder
                # that will upgrade to mp4 in 60-90s. Don't stability-exit
                # on it — keep waiting until either the video upgrade is
                # observed (early-exit branch above) or ``timeout_sec``
                # is reached. Otherwise we'd "succeed" by downloading a
                # 23 KB JPEG poster and never see the actual video.
                in_video_mode = (
                    self._flow_mode_spec is not None
                    and getattr(self._flow_mode_spec, "tab", None) == "video"
                )
                has_images = any(kind == "image" for _, kind in new_only)
                has_videos = any(kind == "video" for _, kind in new_only)
                only_images = has_images and not has_videos
                # In video mode an image-only set is a poster still
                # waiting to upgrade — keep waiting until either it
                # upgrades to video (early-exit branch above) or the
                # round ``timeout_sec`` deadline hits.
                if in_video_mode and only_images:
                    if (time.time() - last_change_time) % 30 < 2.5:
                        _LOG.info(
                            "[round] video mode + %d image-only candidate(s) "
                            "— waiting for poster→mp4 upgrade",
                            len(new_only),
                        )
                elif (time.time() - last_change_time) >= stability_window_sec:
                    # In video mode, drop lingering image candidates from
                    # a mixed set — the videos already there are real
                    # output, the still-image is a stale poster of a
                    # generation that never finished encoding (observed
                    # in the wild after ~5min). Accepting it would write
                    # a 22 KB JPEG into the .mp4 slot.
                    accepted = (
                        [(s, k) for s, k in new_only if k == "video"]
                        if (in_video_mode and has_videos and has_images)
                        else new_only
                    )
                    if (in_video_mode and has_videos and has_images):
                        _LOG.info(
                            "[round] dropped %d stale poster(s) from mixed set "
                            "after %ds stable",
                            len(new_only) - len(accepted),
                            int(time.time() - last_change_time),
                        )
                    candidates = [
                        CandidateMeta(
                            sequence_no=i + 1,
                            download_handle=src,
                            media_kind=kind,
                        )
                        for i, (src, kind) in enumerate(accepted)
                    ]
                    _LOG.info(
                        "round produced %d new candidate(s) (kinds=%s, stability-exit)",
                        len(candidates),
                        {c.media_kind for c in candidates},
                    )
                    return GenerationRoundResult(state=PageState.READY, candidates=candidates)
            elif new_only != last_new:
                last_new = new_only
                last_change_time = time.time()
            time.sleep(2.0)

        return GenerationRoundResult(
            state=PageState.PAGE_LOAD_FAILED,
            candidates=[],
            error_message="generation wait timeout",
            timed_out=True,
        )

    def _collect_candidates_full(self) -> dict[str, str]:
        """Return ``{src: media_kind}`` for every real candidate in DOM,
        deduplicated by Flow's ``name=<UUID>`` query parameter.

        Walks the gallery's ``<video>`` and ``<img>`` elements, keeps only
        those whose ``src`` matches ``candidate_src_pattern`` AND does not
        contain ``MEDIA_URL_TYPE_THUMBNAIL``.

        Crucial: Veo renders a *poster image* (``<img>``) first while the
        video is still encoding, then mounts a ``<video>`` element with
        the SAME ``name`` UUID once encoding finishes. If we treated the
        poster as a separate candidate we'd return early with a tiny
        JPEG instead of the actual mp4. So when both an ``<img>`` and a
        ``<video>`` reference the same UUID, we keep only the ``<video>``
        entry — the worker downloads that URL via tRPC and gets the
        real bytes.
        """
        # Per-UUID best entry: video wins over image.
        by_name: dict[str, tuple[str, str]] = {}
        container_sel = 'div[data-testid="virtuoso-item-list"]'
        for tag, kind in (("video", "video"), ("img", "image")):
            sel = f"{container_sel} {tag}"
            items = self._page.locator(sel)
            try:
                count = items.count()
            except Exception:  # noqa: BLE001
                continue
            for i in range(min(count, 300)):
                try:
                    src = items.nth(i).get_attribute("src")
                except Exception:  # noqa: BLE001
                    continue
                if not src:
                    continue
                if not self._candidate_src_re.search(src):
                    continue
                if "MEDIA_URL_TYPE_THUMBNAIL" in src:
                    continue
                m = _NAME_PARAM_RE.search(src)
                if m is None:
                    # No identifiable name — fall back to dedup-by-src.
                    by_name.setdefault(src, (src, kind))
                    continue
                name = m.group(1)
                existing = by_name.get(name)
                if existing is None:
                    by_name[name] = (src, kind)
                elif existing[1] == "image" and kind == "video":
                    # Upgrade: video supersedes image for the same UUID.
                    by_name[name] = (src, kind)
        return {src: kind for src, kind in by_name.values()}

    def download_candidate(self, candidate: CandidateMeta, target_path: Path) -> None:
        """Fetch the candidate's media URL via the authenticated request context.

        ``download_handle`` is the ``src`` URL collected during the round;
        we resolve it against the current page URL (it's typically a
        relative ``/fx/api/trpc/...`` path) and GET it. Cookies from the
        persistent context follow automatically — Flow's tRPC redirect
        will then 302 to the actual media bytes.
        """
        self._require_page()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        src = candidate.download_handle
        if not isinstance(src, str) or not src:
            raise FlowPortError("download_handle must be a non-empty src URL")

        if src.startswith("data:"):
            self._save_data_uri(src, target_path)
            return

        full_url = src
        if src.startswith("/"):
            from urllib.parse import urljoin
            full_url = urljoin(self._page.url, src)

        try:
            response = self._page.request.get(
                full_url, timeout=self._cfg.timeouts.download * 1000
            )
        except Exception as exc:
            raise FlowPortError(f"download request failed: {exc}") from exc
        if not response.ok:
            raise FlowPortError(
                f"download HTTP {response.status} for {full_url}"
            )
        body = response.body() or b""
        try:
            ct = (response.headers or {}).get("content-type", "<unknown>")
        except Exception:  # noqa: BLE001
            ct = "<unknown>"
        # Sanity check: tRPC ``getMediaUrlRedirect`` should either 302 to a
        # CDN media URL (Playwright follows automatically) or stream bytes.
        # If we got HTML/JSON instead, something authentication-shaped is
        # wrong — bail loudly so it shows up as a real download_failed.
        if len(body) < 1024:
            raise FlowPortError(
                f"download too small ({len(body)} bytes, content-type={ct}) "
                f"-- got {body[:120]!r} from {full_url}"
            )
        if ct.startswith("text/") or ct.startswith("application/json"):
            raise FlowPortError(
                f"download content-type={ct} (expected video/* or image/*); "
                f"first bytes: {body[:120]!r}"
            )
        _LOG.info(
            "downloaded %d bytes content-type=%s -> %s",
            len(body), ct, target_path,
        )
        target_path.write_bytes(body)

    @staticmethod
    def _save_data_uri(src: str, target_path: Path) -> None:
        """Decode a ``data:`` URI body to disk (used by local-HTML tests)."""
        import base64
        from urllib.parse import unquote_to_bytes

        head, _, body = src.partition(",")
        if ";base64" in head:
            target_path.write_bytes(base64.b64decode(body))
        else:
            target_path.write_bytes(unquote_to_bytes(body))

    def take_screenshot(self, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if self._page is None:
            target_path.write_bytes(b"")
            return
        try:
            self._page.screenshot(path=str(target_path), full_page=True)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("screenshot failed: %s", exc)

    # --- helpers ---------------------------------------------------------

    def _require_page(self) -> None:
        if self._page is None:
            raise FlowPortError("FlowPort.open() was not called")


# Convenience helper used by the ``login-flow`` CLI subcommand.

@contextmanager
def open_login_helper(
    *, profile_path: Path, entry_url: str, headless: bool = False
) -> Iterator[object]:
    """Open a persistent browser context so a human can log in.

    Yields the Playwright ``Page``; the caller is expected to ``input()`` /
    wait for user input before closing.
    """
    from patchright.sync_api import sync_playwright  # type: ignore

    profile_path.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    try:
        # Minimal config matches PlaywrightFlowPort.open() — patchright
        # adds its own anti-detection patches; stacking custom UA / args /
        # init scripts on top causes ERR_CONNECTION_CLOSED on goto.
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            channel="chrome",
            headless=headless,
            no_viewport=True,
            chromium_sandbox=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(entry_url, wait_until="domcontentloaded")
        try:
            yield page
        finally:
            ctx.close()
    finally:
        pw.stop()

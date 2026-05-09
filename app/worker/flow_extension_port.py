"""V2 ExtensionFlowPort — implements ``FlowPort`` by talking to the
in-browser chrome extension over the spike WebSocket RPC channel.

Spike-only. The whole module is opt-in via two env vars:

* ``FLOW_HARVESTER_SPIKE_EXTENSION=1`` mounts the WS endpoint
  (``app/web/routes/extension_ws.py``).
* ``FLOW_HARVESTER_USE_EXTENSION=1`` makes ``runner.multi`` /
  ``runner.single`` instantiate this class instead of
  ``PlaywrightFlowPort``.

The point: V1's already-shipped ``execute_task`` / scheduler /
strike / state-machine logic stays intact (1900 lines untouched);
only the page-driver implementation is swapped, so a real customer
task dispatched from the V1 dashboard exercises the V2 architecture
end-to-end.

Worker threads call into this class synchronously (because V1's
runner uses ``ThreadPoolExecutor``); the actual WS round-trip is
asyncio. We bridge with ``asyncio.run_coroutine_threadsafe`` against
the FastAPI main event loop, which ``server.py`` registers via
``set_runtime_loop`` during ``lifespan`` startup.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from app.worker.flow_port import (
    CandidateMeta,
    FlowPortError,
    GenerationRoundResult,
    PageState,
    SourceAsset,
)
from app.worker.flow_selectors import FlowSelectorsConfig, load_flow_selectors


log = logging.getLogger("flow_harvester.worker.extension_port")


# Set from server.py lifespan when FLOW_HARVESTER_USE_EXTENSION=1.
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_runtime_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _MAIN_LOOP
    _MAIN_LOOP = loop
    log.info("[extension_port] main event loop registered")


def get_runtime_loop() -> asyncio.AbstractEventLoop:
    if _MAIN_LOOP is None:
        raise FlowPortError(
            "ExtensionFlowPort runtime loop not initialised — "
            "set FLOW_HARVESTER_SPIKE_EXTENSION=1 + FLOW_HARVESTER_USE_EXTENSION=1 "
            "and restart the server"
        )
    return _MAIN_LOOP


# ---------------------------------------------------------------------------
# Selector translation: V1 playwright `:has-text("X")` → V2 SelectorSpec
# ---------------------------------------------------------------------------

_HAS_TEXT_RE = re.compile(r':has-text\(\s*"([^"]*)"\s*\)')
_TEXT_ONLY_RE = re.compile(r'^\s*text\s*=\s*"([^"]*)"\s*$')


def translate_pw_selector(sel: str) -> dict[str, Any]:
    """Translate a V1 playwright selector to chrome SelectorSpec.

    Handles the two forms V1 actually uses:
      ``button:has-text("arrow_forward"):has-text("Create")``
        →  {"css": "button", "contains_all_text": ["arrow_forward", "Create"]}
      ``text="Start"`` (anywhere in the page)
        →  {"css": "*", "contains_all_text": ["Start"]}

    Anything more exotic falls through as a raw CSS string with no
    text filter; chrome's ``querySelectorAll`` may reject it, in
    which case the helper logs and the round-robin moves on.
    """
    text_only = _TEXT_ONLY_RE.match(sel)
    if text_only is not None:
        return {"css": "*", "contains_all_text": [text_only.group(1)]}
    texts = _HAS_TEXT_RE.findall(sel)
    css = _HAS_TEXT_RE.sub("", sel).strip()
    if not css:
        css = "*"
    return {"css": css, "contains_all_text": texts}


def translate_pw_selectors(selectors: Iterable[str]) -> list[dict[str, Any]]:
    specs = [translate_pw_selector(s) for s in selectors]
    # V1 patchright `:has-text` + `.first` happens to break ties toward
    # the Create button on Flow even when multiple buttons share a
    # `arrow_forward` icon ligature (model picker, nav back, etc.).
    # chrome's querySelectorAll doesn't have that quirk — it walks DOM
    # order, so the icon-only selector at index 0 often matches the
    # WRONG button (silently — no exception, just a click that does
    # nothing). Rank specs with more required-text fragments first so
    # the `arrow_forward + Create / 创建 / Tạo / ...` variants try
    # before the bare-icon fallback.
    specs.sort(key=lambda s: -(len(s.get("contains_all_text") or [])))
    return specs


# ---------------------------------------------------------------------------
# ExtensionFlowPort
# ---------------------------------------------------------------------------


class ExtensionFlowPort:
    """V1's FlowPort, backed by the V2 chrome extension."""

    # WS round-trip envelope timeout (seconds). Per-method overrides via
    # ``timeout_sec`` arg — wait_for_round_complete / download_candidate
    # in particular need much longer than the page-action default.
    DEFAULT_RPC_TIMEOUT_SEC = 60.0

    def __init__(
        self,
        *,
        ws_id: str,
        project_url: str | None,
        page_action_timeout_sec: int = 60,
        selectors_path: Path | None = None,
        candidate_stability_window_sec: float = 30.0,
        flow_mode_spec: object | None = None,
    ) -> None:
        self.ws_id = ws_id
        self.project_url = project_url
        self.page_action_timeout_sec = page_action_timeout_sec
        self.selectors_path = selectors_path or Path("config/flow-selectors.yaml")
        self._cfg: FlowSelectorsConfig = load_flow_selectors(self.selectors_path)
        self._candidate_stability_window_sec = candidate_stability_window_sec
        self._flow_mode_spec = flow_mode_spec

        # Pre-translated SelectorSpecs (avoid re-parsing on every call).
        self._gen_specs = translate_pw_selectors(self._cfg.selectors.generate_button)

        # Captured at the start of trigger_generation, consumed by
        # wait_for_round_complete (V1's PlaywrightFlowPort
        # ``_candidate_snapshot`` equivalent).
        self._baseline_srcs: list[str] = []

    # --- helpers ---------------------------------------------------------

    def _call(
        self,
        method: str,
        args: dict[str, Any] | None = None,
        *,
        target_url: str | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        loop = get_runtime_loop()
        from app.web.routes.extension_ws import call_rpc

        actual_timeout = timeout_sec or self.DEFAULT_RPC_TIMEOUT_SEC
        coro = call_rpc(
            ws_id=self.ws_id,
            method=method,
            target_url=target_url,
            args=args or {},
            timeout_sec=actual_timeout,
        )
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        # Block worker thread until the WS reply is in. Add a buffer over
        # the in-loop timeout so we always raise our own clearer error
        # rather than the asyncio TimeoutError.
        try:
            result = future.result(timeout=actual_timeout + 15.0)
        except Exception as e:  # noqa: BLE001 — propagate as FlowPortError
            raise FlowPortError(f"rpc {method} failed: {e}") from e

        if not result.get("ok"):
            err = result.get("error") or "unknown error"
            raise FlowPortError(f"rpc {method} reported failure: {err}")
        return result

    @staticmethod
    def _payload_data(result: dict[str, Any]) -> dict[str, Any] | None:
        payload = result.get("payload")
        if not isinstance(payload, dict):
            return None
        # Helpers wrap their success payload as {ok: true, data: {...}}
        if isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload

    @staticmethod
    def _phrase_in(haystack: str, phrases: list[str]) -> bool:
        if not haystack:
            return False
        lower = haystack.lower()
        return any(p.lower() in lower for p in phrases)

    def _classify(self, body_text: str) -> PageState:
        # Order matches V1 PlaywrightFlowPort._classify_state intent:
        # account-level signals first (no_flow_access overrides everything),
        # then service-level, then operational warnings.
        phrases = self._cfg.state_phrases
        if self._phrase_in(body_text, phrases.no_flow_access):
            return PageState.NO_FLOW_ACCESS
        if self._phrase_in(body_text, phrases.service_unavailable):
            return PageState.SERVICE_UNAVAILABLE
        if self._phrase_in(body_text, phrases.unusual_activity):
            return PageState.UNUSUAL_ACTIVITY
        if self._phrase_in(body_text, phrases.captcha_or_verification):
            return PageState.CAPTCHA_OR_VERIFICATION
        if self._phrase_in(body_text, phrases.login_required):
            return PageState.LOGIN_REQUIRED
        if self._phrase_in(body_text, phrases.page_load_failed):
            return PageState.PAGE_LOAD_FAILED
        return PageState.READY

    # --- FlowPort surface -----------------------------------------------

    def open(self) -> PageState:
        if not self.project_url:
            raise FlowPortError("ExtensionFlowPort.open(): workstation has no project_url")
        log.info("[extension_port] open ws=%s url=%s", self.ws_id, self.project_url)
        result = self._call(
            "read_page_state",
            target_url=self.project_url,
            timeout_sec=float(self.page_action_timeout_sec),
        )
        data = self._payload_data(result) or {}
        body_text = str(data.get("body_text_snippet") or "")
        url_now = str(data.get("url") or "")
        state = self._classify(body_text)
        log.info(
            "[extension_port] open → state=%s landing_url=%s body_len=%s",
            state.value,
            url_now,
            data.get("body_text_length"),
        )
        return state

    def upload_source_assets(self, assets: list[SourceAsset]) -> None:
        if not assets:
            return
        log.info("[extension_port] upload_source_assets ws=%s count=%d", self.ws_id, len(assets))
        # V1's prompt-attach dialog opening is non-trivial (4 click
        # strategies, dialog wait, library lookup). Spike phase: for the
        # canonical single-asset Veo prompt input, we drop the file
        # straight onto Flow's hidden ``upload_button`` selector. If
        # that fails, the operator's first signal is a meaningful
        # "no file input matched" error rather than a silent timeout.
        file_input_selector = self._cfg.selectors.upload_button
        for asset in assets:
            asset_path = Path(asset.path).resolve()
            if not asset_path.exists():
                raise FlowPortError(f"asset file missing: {asset_path}")
            from urllib.parse import quote

            image_url = (
                "http://127.0.0.1:8080/spike/extension/file?abs="
                + quote(str(asset_path), safe="")
            )
            args = {
                "image_url": image_url,
                "selector": file_input_selector,
                "filename": asset_path.name,
            }
            attach_result = self._call(
                "attach_image",
                args,
                timeout_sec=max(60.0, float(self.page_action_timeout_sec) + 30),
            )
            attach_data = self._payload_data(attach_result) or {}
            log.info(
                "[extension_port] attached asset order=%s kind=%s name=%s "
                "matched_selector=%r files_count=%s size=%s",
                asset.order,
                asset.kind,
                asset_path.name,
                attach_data.get("matched_selector"),
                attach_data.get("files_count"),
                attach_data.get("size"),
            )
            # Flow's React/Slate stack needs a beat to ingest the change
            # event, draw the thumbnail, and unblock the generate button.
            # V1 patchright effectively gets this latency for free
            # because keyboard.type is naturally slow (60-110ms × N
            # chars); chrome page-world events fire instantly so we have
            # to add the wait explicitly.
            time.sleep(2.0)

    def paste_prompt(self, prompt: str) -> None:
        log.info("[extension_port] paste_prompt ws=%s len=%d", self.ws_id, len(prompt))
        result = self._call(
            "paste_prompt",
            {
                "selector": self._cfg.selectors.prompt_input,
                "prompt": prompt,
            },
            timeout_sec=float(self.page_action_timeout_sec),
        )
        data = self._payload_data(result) or {}
        final_value = str(data.get("final_value") or "")
        log.info(
            "[extension_port] paste_prompt → matched=%r path=%s element_tag=%s final_value_len=%d preview=%r",
            data.get("matched_selector"),
            data.get("path"),
            data.get("element_tag"),
            len(final_value),
            final_value[:80],
        )
        if not final_value.strip():
            raise FlowPortError(
                f"paste_prompt: editor still empty after insert (path={data.get('path')!r}, "
                f"selector={data.get('matched_selector')!r}). Slate may have rejected the "
                "synthetic input event. Check the extension SW console."
            )
        # Slate dispatches its onChange asynchronously and the generate
        # button only enables after the editor's internal state catches
        # up. Without this pause, trigger_generation runs while the
        # button is still aria-disabled (or the wrong button is the
        # first hit) and Flow silently ignores the click.
        time.sleep(2.0)

    def trigger_generation(self) -> None:
        # Snapshot the candidate set BEFORE clicking Create so
        # wait_for_round_complete can diff against it. V1 keeps this
        # snapshot inside PlaywrightFlowPort; we keep it inside the
        # python port too — extension is stateless across RPCs.
        scrape = self._call(
            "scrape_candidates",
            {},
            timeout_sec=float(self.page_action_timeout_sec),
        )
        scrape_data = self._payload_data(scrape) or {}
        srcs = scrape_data.get("srcs")
        self._baseline_srcs = list(srcs) if isinstance(srcs, list) else []
        log.info(
            "[extension_port] trigger_generation ws=%s baseline=%d total_videos=%s",
            self.ws_id,
            len(self._baseline_srcs),
            scrape_data.get("total_videos"),
        )

        click = self._call(
            "trigger_generation",
            {"selectors": self._gen_specs},
            timeout_sec=float(self.page_action_timeout_sec),
        )
        click_data = self._payload_data(click) or {}
        log.info(
            "[extension_port] trigger_generation matched_index=%s matched_css=%r matched_texts=%s "
            "tag=%s aria=%r snippet=%r candidates_seen=%s",
            click_data.get("matched_index"),
            click_data.get("matched_css"),
            click_data.get("matched_texts"),
            click_data.get("element_tag"),
            click_data.get("aria_label"),
            (click_data.get("text_snippet") or "")[:120],
            click_data.get("candidates_seen"),
        )

    def wait_for_round_complete(
        self,
        timeout_sec: int,
        *,
        expected_count: Optional[int] = None,
    ) -> GenerationRoundResult:
        # V1 also pulls expected_count off self._flow_mode_spec when
        # missing; replicate so multi-round / x{N} settings still work.
        if expected_count is None and self._flow_mode_spec is not None:
            spec_count = getattr(self._flow_mode_spec, "output_count", None)
            if isinstance(spec_count, int) and spec_count > 0:
                expected_count = spec_count
        expected_count = expected_count or 1

        log.info(
            "[extension_port] wait_for_round_complete ws=%s expected=%d timeout=%d",
            self.ws_id,
            expected_count,
            timeout_sec,
        )
        round_started = time.time()
        result = self._call(
            "wait_round_complete",
            {
                "container_selector": 'div[data-testid="virtuoso-item-list"]',
                "src_pattern": r"media\.getMediaUrlRedirect",
                "baseline_srcs": list(self._baseline_srcs),
                "expected_count": expected_count,
                "timeout_sec": int(max(1, timeout_sec)),
                "stability_window_sec": float(self._candidate_stability_window_sec),
                "poll_interval_ms": 1000,
            },
            timeout_sec=float(timeout_sec) + 30.0,
        )
        data = self._payload_data(result) or {}
        new_srcs = data.get("new_srcs") or []
        timed_out = bool(data.get("timed_out"))
        elapsed = time.time() - round_started
        log.info(
            "[extension_port] wait_for_round_complete done srcs=%d timed_out=%s elapsed=%.1fs",
            len(new_srcs),
            timed_out,
            elapsed,
        )

        # No phrase classification on the wait-output yet (spike scope).
        # An empty new_srcs + timed_out trips V1's no-progress branch
        # which is equivalent to "Veo didn't produce anything".
        candidates: list[CandidateMeta] = []
        for idx, src in enumerate(new_srcs):
            if not isinstance(src, str):
                continue
            candidates.append(
                CandidateMeta(sequence_no=idx + 1, download_handle=src, media_kind="video")
            )

        return GenerationRoundResult(
            state=PageState.READY,
            candidates=candidates,
            error_message=None,
            timed_out=timed_out and not candidates,
        )

    def download_candidate(self, candidate: CandidateMeta, target_path: Path) -> None:
        if not isinstance(candidate.download_handle, str):
            raise FlowPortError(
                f"download_candidate: handle is not a URL: {candidate.download_handle!r}"
            )
        src_url = candidate.download_handle
        # chrome.downloads writes under chrome's default Downloads root.
        # We use a unique path so two concurrent downloads can't clash,
        # then move the actual file (which chrome reports back via
        # ``filename``) to V1's intended absolute path.
        spike_rel = (
            f"FlowHarvester/v2spike/{self.ws_id}/{uuid.uuid4().hex}{target_path.suffix or '.mp4'}"
        )
        result = self._call(
            "download_video",
            {
                "url": src_url,
                "filename": spike_rel,
                "conflict_action": "uniquify",
            },
            timeout_sec=180.0,
        )
        data = self._payload_data(result) or {}
        chrome_actual = data.get("filename")
        if not isinstance(chrome_actual, str) or not chrome_actual:
            raise FlowPortError(f"download_candidate: missing filename in payload: {data!r}")
        chrome_path = Path(chrome_actual)
        if not chrome_path.exists():
            raise FlowPortError(
                f"download_candidate: chrome reported {chrome_actual} but file is missing"
            )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(chrome_path), str(target_path))
        except OSError as e:
            raise FlowPortError(
                f"download_candidate: move {chrome_path} → {target_path} failed: {e}"
            ) from e
        log.info(
            "[extension_port] download_candidate seq=%d size=%s → %s",
            candidate.sequence_no,
            data.get("file_size"),
            target_path,
        )

    def take_screenshot(self, target_path: Path) -> None:
        result = self._call("take_screenshot", {}, timeout_sec=30.0)
        # take_screenshot's payload is flat (not wrapped in {ok,data}).
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise FlowPortError(f"take_screenshot bad payload: {payload!r}")
        data_url = payload.get("data_url")
        if not isinstance(data_url, str):
            raise FlowPortError("take_screenshot: payload missing data_url")
        prefix = "data:image/jpeg;base64,"
        if not data_url.startswith(prefix):
            raise FlowPortError(f"take_screenshot: unexpected data_url prefix")
        b64 = data_url[len(prefix):]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            target_path.write_bytes(base64.b64decode(b64))
        except OSError as e:
            raise FlowPortError(f"take_screenshot: write {target_path} failed: {e}") from e
        log.debug("[extension_port] screenshot → %s (%d bytes)", target_path, target_path.stat().st_size)

    def close(self) -> None:
        # Extension SW + chrome tab stay alive across tasks (chrome
        # profile is the long-running unit, not the port). V1's
        # PlaywrightFlowPort.close() tears down the patchright runtime,
        # which has no analogue here.
        log.debug("[extension_port] close ws=%s (no-op)", self.ws_id)

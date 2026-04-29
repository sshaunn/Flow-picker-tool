"""Mock FlowPort used by tests and ``--mock`` CLI runs (T07-T10, T18).

Behaviour is fully scripted by the constructor: callers feed a list of
"round plans". Each round plan declares either a successful round (with N
candidates) or an exception. This lets us cover:

* normal happy path, target reached after N rounds,
* unusual_activity / login_required mid-round,
* download_failed for individual sequences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from app.worker.flow_port import (
    CandidateMeta,
    FlowPort,
    FlowPortError,
    GenerationRoundResult,
    PageState,
    SourceAsset,
)


@dataclass
class _MockCandidate:
    sequence_no: int
    download_succeeds: bool = True
    payload: bytes = b"fake-mp4-bytes"


@dataclass
class MockRoundPlan:
    state: PageState = PageState.READY
    candidates: list[_MockCandidate] = field(default_factory=list)
    error_message: str | None = None
    timed_out: bool = False
    initial_state: PageState | None = None  # state to return from open() before this round

    @classmethod
    def success(cls, num: int, *, all_download_ok: bool = True) -> "MockRoundPlan":
        return cls(
            state=PageState.READY,
            candidates=[_MockCandidate(seq, all_download_ok) for seq in range(1, num + 1)],
        )

    @classmethod
    def partial_download(cls, num_total: int, num_failing: int) -> "MockRoundPlan":
        cands = [_MockCandidate(seq, True) for seq in range(1, num_total + 1)]
        for i in range(num_failing):
            cands[-1 - i] = _MockCandidate(cands[-1 - i].sequence_no, False)
        return cls(state=PageState.READY, candidates=cands)

    @classmethod
    def all_downloads_fail(cls, num_total: int) -> "MockRoundPlan":
        return cls(
            state=PageState.READY,
            candidates=[_MockCandidate(seq, False) for seq in range(1, num_total + 1)],
        )

    @classmethod
    def page_error(cls, state: PageState, message: str = "mock error") -> "MockRoundPlan":
        return cls(state=state, error_message=message)

    @classmethod
    def timeout(cls, message: str = "mock timeout") -> "MockRoundPlan":
        return cls(state=PageState.PAGE_LOAD_FAILED, error_message=message, timed_out=True)


class MockFlowPort(FlowPort):
    """Scripted FlowPort. Drives the worker through the supplied round plans."""

    def __init__(
        self,
        round_plans: list[MockRoundPlan],
        *,
        initial_state: PageState = PageState.READY,
        screenshot_writer: Callable[[Path], None] | None = None,
    ) -> None:
        self._initial_state = initial_state
        self._plans: Iterator[MockRoundPlan] = iter(round_plans)
        self._current_plan: MockRoundPlan | None = None
        self._screenshot_writer = screenshot_writer or _default_screenshot_writer
        # Each call records the *list* of assets received (preserving order
        # and kind), so tests can assert multi-asset behaviour.
        self.upload_calls: list[list[SourceAsset]] = []
        self.prompt_calls: list[str] = []
        self.trigger_calls: int = 0
        self.closed: bool = False

    def open(self) -> PageState:
        return self._initial_state

    def upload_source_assets(self, assets: list[SourceAsset]) -> None:
        self.upload_calls.append(list(assets))

    def paste_prompt(self, prompt: str) -> None:
        self.prompt_calls.append(prompt)

    def trigger_generation(self) -> None:
        self.trigger_calls += 1
        try:
            self._current_plan = next(self._plans)
        except StopIteration as exc:
            raise FlowPortError("MockFlowPort: no more round plans available") from exc

    def wait_for_round_complete(self, timeout_sec: int) -> GenerationRoundResult:
        plan = self._current_plan
        if plan is None:
            raise FlowPortError("wait_for_round_complete called before trigger_generation")
        candidates = [
            CandidateMeta(sequence_no=c.sequence_no, download_handle=c)
            for c in plan.candidates
        ]
        return GenerationRoundResult(
            state=plan.state,
            candidates=candidates,
            error_message=plan.error_message,
            timed_out=plan.timed_out,
        )

    def download_candidate(self, candidate: CandidateMeta, target_path: Path) -> None:
        handle = candidate.download_handle
        if not isinstance(handle, _MockCandidate):
            raise FlowPortError("MockFlowPort: malformed candidate handle")
        if not handle.download_succeeds:
            raise FlowPortError(
                f"mock download failure for sequence_no={candidate.sequence_no}"
            )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(handle.payload)

    def take_screenshot(self, target_path: Path) -> None:
        self._screenshot_writer(target_path)

    def close(self) -> None:
        self.closed = True


def _default_screenshot_writer(target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(b"\x89PNG\r\n\x1a\nMOCK")

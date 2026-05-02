"""Login session unit tests + login API tests.

These tests don't actually launch a browser — instead they mock the
``patchright.sync_api.sync_playwright`` import inside the session
thread so we can drive ``page.url`` deterministically and verify the
state machine + capture callback flow.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import paths as app_paths
from app.web.server import create_app
from app.workstations.login_session import (
    PROJECT_URL_RE,
    LoginSession,
    LoginSessionRegistry,
    LoginState,
)


def _wait_for(predicate, timeout: float = 3.0, poll: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return predicate()


# --------------------------------------------------------------- regex sanity


def test_project_url_regex_matches_expected_shape() -> None:
    url = "https://labs.google/fx/tools/flow/project/bf3454e4-98c9-4528-a762-c39087797014"
    m = PROJECT_URL_RE.match(url)
    assert m is not None
    assert m.group(0) == url


def test_project_url_regex_rejects_non_project_pages() -> None:
    assert PROJECT_URL_RE.match("https://labs.google/fx/tools/flow") is None
    assert PROJECT_URL_RE.match("https://accounts.google.com/...") is None


# ----------------------------------------------------- mocked patchright tests


class _MockPage:
    """Drives ``page.url`` and ``page.evaluate(...)`` from a list of return
    values, advancing one position per read so the test can simulate a
    sequence of navigations."""

    def __init__(self, urls: list[str]) -> None:
        self._urls = urls
        self._idx = 0

    @property
    def url(self) -> str:
        url = self._urls[min(self._idx, len(self._urls) - 1)]
        self._idx += 1
        return url

    def evaluate(self, _expr: str):
        # Mirror the property so the production-side eval fallback is
        # exercised; tests don't care which source caught the URL.
        return self._urls[min(self._idx, len(self._urls) - 1)]

    def goto(self, *args, **kwargs) -> None:
        return None

    def bring_to_front(self) -> None:
        return None

    def on(self, _event, _handler) -> None:
        return None


class _MockContext:
    def __init__(self, page: _MockPage) -> None:
        self.pages = [page]
        self.closed = False

    def new_page(self) -> _MockPage:
        return self.pages[0]

    def on(self, _event, _handler) -> None:
        return None

    def close(self) -> None:
        self.closed = True


@contextmanager
def _mock_patchright(urls: list[str]):
    """Patch ``patchright.sync_api.sync_playwright`` to return a context
    manager whose chromium driver hands out the predetermined ``page.url``
    sequence."""
    page = _MockPage(urls)
    ctx = _MockContext(page)
    chromium = MagicMock()
    chromium.launch_persistent_context.return_value = ctx
    pw = MagicMock(chromium=chromium)
    pw_cm = MagicMock(__enter__=MagicMock(return_value=pw),
                      __exit__=MagicMock(return_value=None))
    factory = MagicMock(return_value=pw_cm)
    with patch("patchright.sync_api.sync_playwright", factory):
        yield ctx, page


def test_session_captures_project_url_via_state_machine(tmp_path: Path) -> None:
    captured = []
    urls = [
        "https://labs.google/fx/tools/flow",  # waiting_for_login → project
        "https://labs.google/fx/tools/flow",
        "https://labs.google/fx/tools/flow/project/bf3454e4-98c9-4528-a762-c39087797014",
    ]
    with _mock_patchright(urls) as (ctx, page):
        session = LoginSession(
            workstation_id="WS_TEST",
            profile_path=tmp_path / "prof",
            entry_url="https://labs.google/fx/tools/flow",
            on_capture=lambda u: captured.append(u),
            poll_interval_sec=0.01,
        )
        session.start()
        ok = _wait_for(lambda: session.status().state == LoginState.CAPTURED, timeout=2.0)
    assert ok
    snap = session.status()
    assert snap.captured_url == urls[-1]
    assert captured == [urls[-1]]
    assert ctx.closed is True


def test_session_cancel_terminates_thread(tmp_path: Path) -> None:
    # Stays at the tools home forever; cancel must wake the loop.
    urls = ["https://labs.google/fx/tools/flow"] * 200
    with _mock_patchright(urls):
        session = LoginSession(
            workstation_id="WS_CANCEL",
            profile_path=tmp_path / "prof",
            entry_url="https://labs.google/fx/tools/flow",
            on_capture=lambda u: None,
            poll_interval_sec=0.05,
        )
        session.start()
        _wait_for(lambda: session.status().state == LoginState.WAITING_FOR_PROJECT)
        ok = session.cancel(timeout=2.0)
    assert ok
    assert session.status().state == LoginState.CANCELLED


def test_session_capture_callback_failure_yields_error(tmp_path: Path) -> None:
    urls = [
        "https://labs.google/fx/tools/flow/project/abcdef12-3456-7890-abcd-ef1234567890",
    ]

    def boom(_url: str) -> None:
        raise RuntimeError("db unavailable")

    with _mock_patchright(urls):
        session = LoginSession(
            workstation_id="WS_ERR",
            profile_path=tmp_path / "prof",
            entry_url="https://labs.google/fx/tools/flow",
            on_capture=boom,
            poll_interval_sec=0.01,
        )
        session.start()
        _wait_for(lambda: session.status().state == LoginState.ERROR, timeout=2.0)
    assert session.status().state == LoginState.ERROR
    assert "db unavailable" in (session.status().error or "")


def test_session_browser_closed_externally_cancels(tmp_path: Path) -> None:
    """When the customer closes the Chrome window, ctx.pages becomes
    empty; the watcher must move to CANCELLED so the UI stops polling."""

    class _ShortLivedPage:
        @property
        def url(self):
            return "https://labs.google/fx/tools/flow"
        def evaluate(self, _expr):
            return "https://labs.google/fx/tools/flow"
        def goto(self, *args, **kwargs): return None
        def bring_to_front(self): return None
        def on(self, *args, **kwargs): return None

    page = _ShortLivedPage()
    pages_list = [page]

    class _ClosingCtx:
        pages = pages_list
        def on(self, *args, **kwargs): return None
        def close(self): pass

    ctx = _ClosingCtx()
    chromium = MagicMock()
    chromium.launch_persistent_context.return_value = ctx
    pw = MagicMock(chromium=chromium)
    pw_cm = MagicMock(__enter__=MagicMock(return_value=pw),
                      __exit__=MagicMock(return_value=None))
    with patch("patchright.sync_api.sync_playwright", MagicMock(return_value=pw_cm)):
        session = LoginSession(
            workstation_id="WS_X",
            profile_path=tmp_path / "prof",
            entry_url="https://labs.google/fx/tools/flow",
            on_capture=lambda u: None,
            poll_interval_sec=0.01,
        )
        session.start()
        # Wait for the watcher to enter waiting state, then drain pages
        # to simulate the customer closing the Chrome window.
        _wait_for(lambda: session.status().state == LoginState.WAITING_FOR_PROJECT,
                  timeout=2.0)
        pages_list.clear()
        _wait_for(lambda: session.status().state == LoginState.CANCELLED, timeout=2.0)
    assert session.status().state == LoginState.CANCELLED


# --------------------------------------------------------- registry behaviour


def test_registry_replaces_session(tmp_path: Path) -> None:
    reg = LoginSessionRegistry()
    s1 = LoginSession(
        workstation_id="W", profile_path=tmp_path / "p",
        entry_url="https://x", on_capture=lambda u: None,
    )
    reg.put(s1)
    assert reg.get("W") is s1
    s2 = LoginSession(
        workstation_id="W", profile_path=tmp_path / "p2",
        entry_url="https://x", on_capture=lambda u: None,
    )
    reg.put(s2)
    assert reg.get("W") is s2


# ----------------------------------------------------- HTTP API + page tests


@pytest.fixture(autouse=True)
def _redirect_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(app_paths._ENV_DATA_DIR, str(tmp_path / "data"))


def _client(app_config) -> TestClient:
    app = create_app(
        config=app_config, auto_start_daemon=False,
        idle_poll_sec=0.05, push_interval_sec=10.0,
        use_mock=True,
    )
    return TestClient(app)


def test_login_status_for_unstarted_session_is_not_started(app_config) -> None:
    with _client(app_config) as client:
        client.post("/api/workstations", json={
            "id": "WS_API", "account_label": "a",
            "browser_profile_path": "/tmp/p", "daily_task_limit": 5,
        })
        resp = client.get("/api/workstations/WS_API/login")
    assert resp.status_code == 200
    assert resp.json()["state"] == "not_started"


def test_login_partial_renders_initial_button(app_config) -> None:
    with _client(app_config) as client:
        client.post("/api/workstations", json={
            "id": "WS_PARTIAL", "account_label": "a",
            "browser_profile_path": "/tmp/p", "daily_task_limit": 5,
        })
        resp = client.get("/workstations/WS_PARTIAL/login/partial")
    assert resp.status_code == 200
    assert "登录" in resp.text
    assert "/api/workstations/WS_PARTIAL/login" in resp.text


def test_login_start_returns_status_and_persists_capture(
    app_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: POST /login starts a (mocked) session, capture writes to DB."""
    # Fresh data dir already isolated by autouse fixture.
    captured_url = "https://labs.google/fx/tools/flow/project/abcdef12-3456-7890-abcd-ef1234567890"
    urls = [
        "https://labs.google/fx/tools/flow",
        captured_url,
    ]
    with _client(app_config) as client:
        client.post("/api/workstations", json={
            "id": "WS_E2E", "account_label": "a",
            "browser_profile_path": str(tmp_path / "prof"),
            "daily_task_limit": 5,
        })
        with _mock_patchright(urls):
            resp = client.post("/api/workstations/WS_E2E/login")
            assert resp.status_code == 202
            # Poll the JSON status until captured.
            ok = _wait_for(
                lambda: client.get("/api/workstations/WS_E2E/login").json()["state"]
                        == "captured",
                timeout=3.0,
            )
            assert ok

            # And the WS row in DB now carries the captured URL + default mode.
            ws_row = client.get("/api/workstations/WS_E2E").json()
    assert ws_row["flow_project_url"] == captured_url
    # Default mode preset applied because the WS had none before.
    assert ws_row["flow_mode"] is not None
    assert ws_row["flow_mode"]["aspect"] == "9:16"


def test_login_cancel_404_when_no_session(app_config) -> None:
    with _client(app_config) as client:
        client.post("/api/workstations", json={
            "id": "WS_NS", "account_label": "a",
            "browser_profile_path": "/tmp/p", "daily_task_limit": 5,
        })
        resp = client.delete("/api/workstations/WS_NS/login")
    assert resp.status_code == 404


def test_login_start_for_unknown_workstation_returns_404(app_config) -> None:
    with _client(app_config) as client:
        resp = client.post("/api/workstations/NOPE/login")
    assert resp.status_code == 404

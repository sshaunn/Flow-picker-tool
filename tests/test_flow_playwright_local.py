"""Real-Playwright smoke test against a local Flow-shaped HTML page.

This test ensures the PlaywrightFlowPort + worker loop work end-to-end
against a *real* Chromium, not just MockFlowPort. The mock HTML mirrors
what we observed on real Flow:

* Hidden ``<input type="file">`` for uploads.
* Slate-style contenteditable for the prompt.
* "Create" button with the ``arrow_forward`` ligature.
* A virtualized-list container (``data-testid="virtuoso-item-list"``)
  containing ``<video>`` elements with ``media.getMediaUrlRedirect`` URLs.
* Pre-existing "historical" videos so the snapshot/diff path actually has
  to filter them out.

Skipped when:
* Playwright python is not installed,
* Chromium binary has not been downloaded.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("patchright.sync_api")


def _chromium_available() -> bool:
    try:
        from patchright.sync_api import sync_playwright

        with sync_playwright() as pw:
            try:
                b = pw.chromium.launch(headless=True)
            except Exception:
                return False
            b.close()
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(),
    reason="Chromium not available (run `patchright install chromium`)",
)


HTML_TEMPLATE = """\
<!doctype html>
<html><head><title>flow mock</title></head>
<body>
  <h1>flow mock</h1>

  <!-- Prompt-attach upload mock. Clicking "+" opens a fake dialog with an
       Upload image label that wraps a hidden file input — set_input_files
       on the input fires Playwright's file chooser. After selection the
       dialog auto-closes. -->
  <button type="button" id="plus-btn" data-state="closed">add_2 Create</button>
  <div role="dialog" id="picker-dialog" style="display:none">
    <label id="upload-image-trigger" for="hidden-upload">Upload image</label>
    <input id="hidden-upload" type="file" style="display:none" />
  </div>

  <div role="textbox" data-slate-editor="true" data-slate-node="value"
       contenteditable="true" style="border:1px solid #ccc; min-height:60px"></div>

  <button type="button" id="gen-btn">arrow_forward Create</button>

  <!-- Virtualised candidate list — pre-seed with two "historical" entries
       so the snapshot/diff path actually has to filter them out. -->
  <div data-testid="virtuoso-item-list">
    <div><span><div><a><button>
      <video src="data:video/mp4;custom=media.getMediaUrlRedirect-hist1;base64,SElTVDE="></video>
    </button></a></div></span></div>
    <div><span><div><a><button>
      <video src="data:video/mp4;custom=media.getMediaUrlRedirect-hist2;base64,SElTVDI="></video>
    </button></a></div></span></div>
  </div>

  <script>
    const plus = document.getElementById('plus-btn');
    const dlg = document.getElementById('picker-dialog');
    plus.addEventListener('click', () => {{
      dlg.setAttribute('data-state', 'open');
      dlg.style.display = 'block';
    }});
    document.getElementById('hidden-upload').addEventListener('change', () => {{
      // Pretend we attached a chip — auto-close the dialog so the
      // adapter's wait_for(state="hidden") passes.
      dlg.setAttribute('data-state', 'closed');
      dlg.style.display = 'none';
    }});

    const N_NEW = {NUM};
    document.getElementById('gen-btn').addEventListener('click', () => {{
      const list = document.querySelector('[data-testid="virtuoso-item-list"]');
      let added = 0;
      const tick = () => {{
        if (added >= N_NEW) return;
        added += 1;
        const seq = added;
        const wrap = document.createElement('div');
        wrap.innerHTML = `
          <span><div><a><button>
            <video data-seq="${{seq}}"></video>
          </button></a></div></span>`;
        const v = wrap.querySelector('video');
        v.src = 'data:video/mp4;custom=media.getMediaUrlRedirect;base64,' +
                btoa('VIDEO-' + seq);
        list.appendChild(wrap);
        setTimeout(tick, 200);
      }};
      tick();
    }});
  </script>
</body></html>
"""


def _write_mock_html(tmp_path: Path, num_candidates: int = 4) -> Path:
    html = HTML_TEMPLATE.replace("{NUM}", str(num_candidates))
    p = tmp_path / "flow_mock.html"
    p.write_text(html, encoding="utf-8")
    return p


def test_playwright_port_runs_one_round_against_local_html(
    tmp_path: Path, db_path: Path, app_config
) -> None:
    from app.db.connection import connect
    from app.worker.flow_playwright import PlaywrightFlowPort
    from app.worker.loop import TaskInput, execute_task

    html_path = _write_mock_html(tmp_path, num_candidates=4)
    profile = tmp_path / "profile_pw"
    profile.mkdir()

    asset = tmp_path / "asset.png"
    asset.write_bytes(b"\x89PNG")
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
            "source_asset_path, source_asset_type, video_prompt, target_count) "
            "VALUES ('T1', 'sku', 'cre', 'A', ?, 'first_frame', 'p', 4)",
            (str(asset),),
        )
    finally:
        conn.close()

    flow = PlaywrightFlowPort(
        entry_url=html_path.absolute().as_uri(),
        profile_path=profile,
        page_action_timeout_sec=15,
        headless=True,
        ensure_video_mode=False,  # local mock has no Video tab
        # Local mock doesn't simulate the Veo poster-image -> mp4-video
        # upgrade, so we don't need the long production window.
        candidate_stability_window_sec=3.0,
    )

    # Local test uses 3s stability window (configured on the port) so a
    # 30s total wait is plenty.
    gen_cfg = app_config.generation.model_copy(update={"generation_wait_timeout_sec": 30})

    log = logging.getLogger("test")
    conn = connect(db_path)
    try:
        outcome = execute_task(
            conn=conn,
            log=log,
            flow=flow,
            workstation_id="WS_A",
            task=TaskInput(
                task_id="T1",
                sku_id="sku",
                creative_id="cre",
                segment_id="A",
                source_asset_path=asset,
                video_prompt="hello prompt",
                target_count=4,
            ),
            config=gen_cfg,
            output_root=Path(app_config.output_root),
            run_date=date(2026, 4, 28),
        )
    finally:
        conn.close()

    assert outcome.final_status == "success", outcome
    # Pre-seeded historical videos must not be counted.
    assert outcome.downloaded_count == 4, outcome
    for c in outcome.candidates_persisted:
        path = Path(c["video_file_path"])
        assert path.exists()
        # Each downloaded file should contain its sequence-tagged payload.
        body = path.read_bytes()
        assert body.startswith(b"VIDEO-")

# Flow Harvester V2 — Chrome Extension (spike)

Spike build for V2 architecture validation. **Not production.** See
[../docs/v2-architecture-design.md](../docs/v2-architecture-design.md)
for the full architecture and acceptance criteria.

## What this validates

Phase A spike step 0 — minimal end-to-end:

1. Chrome loads the unpacked extension from `dist/`.
2. Service worker connects to Co-Pilot at `ws://127.0.0.1:8080/ws/extension/spike-test`.
3. Extension sends `register` → Co-Pilot replies `register_ack`.
4. `chrome.alarms` 30 s heartbeat keeps the WS warm across SW hibernate.
5. From the Co-Pilot dashboard, dispatching a `fake_task_assign` opens the target URL,
   captures the visible tab, and sends the JPEG back as `fake_screenshot`.

Real DOM operations, multi-language selectors, license, multi-tab, and the
rest of design v0.6 are deferred to the next spike steps.

## Build

```bash
cd extension
npm install
npm run build
```

Output: `extension/dist/` (manifest + bundled JS + popup HTML + icons).

For iterative dev:

```bash
npm run dev   # vite watch — rebuilds dist/ on every change
```

## Install in Chrome

1. Start Co-Pilot with the spike feature flag:
   ```bash
   FLOW_HARVESTER_SPIKE_EXTENSION=1 .venv/bin/flow-harvester serve
   ```
2. Open `chrome://extensions` → toggle **Developer mode**.
3. **Load unpacked** → select `extension/dist/`.
4. Click the extension icon — popup should show "online" within ~5 s.

### Multiple accounts (one operator, N profiles)

V2's deployment model is: **one chrome profile per Google account, one
extension copy per profile**. Each profile has independent cookies /
storage / extension SW — chrome's profile boundary is the isolation
mechanism (same as V1 patchright `--user-data-dir`).

1. In chrome, click the avatar (top-right) → **Add** → create a profile
   per account (e.g. *WS_A*, *WS_B*, *WS_C*).
2. In **each** new profile, repeat steps 2–4 above to load `extension/dist/`.
3. The first time the SW boots in a profile, it auto-generates a unique
   `ws_id` (e.g. `WS-3f7a4b2c`) and writes it to that profile's
   `chrome.storage.local`. Profiles do not share storage, so each
   profile gets its own ws_id automatically.
4. (Optional) Click the extension icon → enter a friendlier name (e.g.
   `WS_A`) → **保存并重连**. The SW reconnects to Co-Pilot under the
   new id.
5. Go to `http://127.0.0.1:8080/spike/extension` — you should see one
   row per profile, each with its own ws_id and live screenshot.

Tasks dispatched to `WS_A` only open tabs in *WS_A*'s chrome window —
chrome's `tabs` / `scripting` / `downloads` APIs cannot cross the
profile boundary, so accounts don't pollute each other.

## Verify

* Co-Pilot logs: `[extension_ws] register WS-... ...`
* `curl http://127.0.0.1:8080/healthz` → `{"status":"ok"}` (already in V1, popup uses it)
* Dashboard `/spike/extension` (added in `app/web/routes/extension_ws.py`)
  shows the live WS state and one form per RPC method.

## End-to-end test (real customer flow)

You don't have to wire 8 RPCs by hand — `app/worker/flow_extension_port.py`
already implements V1's `FlowPort` Protocol on top of these RPCs, and
`app/runner/multi.py` / `app/runner/single.py` will pick it up
automatically when the feature flag is on. So the test is simply:
**dispatch a real task from the V1 dashboard, watch V1's full pipeline
drive Flow through the extension**.

### ⚠️ Hard rule during spike: don't click V1 dashboard's 登录 button

V1's login flow (and `PlaywrightFlowPort.open()`) calls
`app/workstations/profile_check.py:clean_profile_lock()` before
launching its own chrome. That function `psutil.kill()`s *every*
process whose cmdline contains the workstation's profile path —
**including the spike chrome you just opened with that same
`--user-data-dir`**.

If your spike chrome dies unexpectedly mid-task, that's the most
likely cause. While the spike is running:

* run V1 login *first*, **let it finish and close on its own**, then
  launch `start-spike-chrome.sh`;
* don't click V1's "登录" again until you're done with the spike.

Production V2 will replace this with a proper chrome_profile_launcher
that owns the chrome lifecycle and coordinates with V1 login so they
can't fight over the user-data-dir.

### Pre-conditions

1. **The V1 login flow has already run** for the workstation you want to
   exercise (e.g. `WS_D`). V1 stores the resulting chrome profile
   (cookies, signed-in Google account, English UI) at:
   ```
   ~/Library/Application Support/FlowHarvester/profiles/WS_D
   ```
   The V2 extension MUST run inside that exact `--user-data-dir`,
   otherwise it sees your everyday Google account and tasks land on
   the wrong Flow project. Use `scripts/start-spike-chrome.sh WS_D`
   below — it wires the user-data-dir + the extension together in one
   command so the two never drift apart.
2. **A real `flow_project_url`** (`https://labs.google/fx/tools/flow/project/<uuid>`)
   bound to that workstation in the V1 DB.
3. **At least one task in the queue** with an asset image.

### Setup

```bash
# 1. Start Co-Pilot with BOTH spike flags on.
FLOW_HARVESTER_SPIKE_EXTENSION=1 \
FLOW_HARVESTER_USE_EXTENSION=1 \
.venv/bin/flow-harvester serve
```

Look for these lines in the log on boot:

```
[spike] V2 extension WS routes mounted (FLOW_HARVESTER_SPIKE_EXTENSION=1)
[spike] ExtensionFlowPort runtime loop registered (FLOW_HARVESTER_USE_EXTENSION=1)
```

```bash
# 2. Build the extension (rebuild whenever you change extension/src/**).
cd extension && npm install && npm run build
```

```bash
# 3. Launch a SEPARATE chrome process bound to the V1-logged-in profile,
#    with the V2 extension auto-loaded. Replaces "load unpacked" — that
#    workflow only loads the extension into your everyday chrome, which
#    has the wrong Google account.
./scripts/start-spike-chrome.sh WS_D
```

This opens a chrome window that:

* uses `~/Library/Application Support/FlowHarvester/profiles/WS_D` as
  its `--user-data-dir` (so the WS_D Google account is already
  signed in),
* has the freshly built `extension/dist/` loaded via `--load-extension`
  (no `chrome://extensions` step needed),
* runs **alongside** your everyday chrome as a separate process tree.

4. **First time only**: in the new chrome, click the Flow Harvester
   icon → set Workstation to exactly `WS_D` (matches V1 DB; underscore
   not hyphen) → 保存并重连. The id is persisted in that profile's
   `chrome.storage.local`, so subsequent launches don't need this step.

5. The dashboard at `http://127.0.0.1:8080/spike/extension` should
   show `WS_D` as online with a fresh heartbeat.

### Run

6. In the V1 dashboard (`http://127.0.0.1:8080/`), make sure the
   workstation is healthy and there's a pending task assigned to it,
   then start the scheduler the usual way.

7. **Watch the chrome window** — the same `flow_project_url` you
   configured opens, the asset uploads, the prompt is typed, Flow
   starts generating, the round completes, and the mp4s are
   downloaded into V1's normal `output/<date>/<sku>/...` tree (chrome
   writes them under `~/Downloads/FlowHarvester/v2spike/<ws_id>/...`,
   then the python port `shutil.move`s them into V1's expected path).

If that finishes without errors, **V2 architecture is end-to-end
viable** — V1's 1900-line `flow_playwright.py` was never invoked, only
its `FlowPort`-shaped twin in the extension was.

### Falling back to V1

Just unset `FLOW_HARVESTER_USE_EXTENSION` and restart. V1's
`PlaywrightFlowPort` runs as before. The extension can still be loaded;
it just has no traffic until you flip the flag back on.

### Known spike limitations (do NOT ship these as-is)

* `upload_source_assets` only handles single-asset (Veo prompt-attach)
  tasks reliably. Frames mode (first-frame + last-frame) and the
  legacy "Add Media" / Ingredients dialog flow are V1-only for now;
  see V1 `_upload_via_prompt_attach` / `_upload_via_frame_buttons` for
  what still needs porting.
* No phrase-based state classification in the middle of
  `wait_for_round_complete` — V1 watches body text mid-wait to flip
  early into UNUSUAL_ACTIVITY / SERVICE_UNAVAILABLE; the extension
  port currently only checks at `open()`.
* `take_screenshot` requires `<all_urls>` (manifest) — see
  Constraints below; production will replace with offscreen +
  html2canvas or `chrome.debugger.Page.captureScreenshot`.
* `/spike/extension/file?abs=<path>` is a path-restricted helper
  (workspace tree only) but still bypasses V1's normal `/files/`
  authentication. Spike-only.

## RPC quick reference

| RPC | Purpose | Implemented via |
|---|---|---|
| `read_page_state` | Sanity probe + body text grab for phrase detection | `chrome.scripting` MAIN world |
| `paste_prompt` | Set textarea/input value (Grammarly + IME defended) | React value setter + dispatchEvent |
| `take_screenshot` | Capture visible tab as JPEG data URL | `chrome.tabs.captureVisibleTab` |
| `trigger_generation` | Click first matching button across 14 multi-lang selectors | `element.click()` in MAIN world |
| `attach_image` | Upload bytes into `<input type=file>` | SW fetch → base64 → DataTransfer |
| `scrape_candidates` | Snapshot current candidate `<video>` srcs | `querySelectorAll` + regex filter |
| `wait_round_complete` | Poll for new srcs + stability window | Loop in MAIN world (V1 logic ported) |
| `download_video` | `chrome.downloads.download` → wait `complete` | `chrome.downloads` + onChanged listener |

## Constraints (spike-only)

* No `key` field in `manifest.json` yet — `ext_id` is path-derived. Fix when we
  cut the V2.0 release branch (design v0.6 §4.2 / C-050).
* No `_locales/` — `default_locale` is omitted in spike to avoid load failure.
* SW in-memory state is wiped on extension reload; **`ws_id` survives** in
  `chrome.storage.local` so the workstation identity is stable across reloads
  and chrome restarts.
* `host_permissions` includes `<all_urls>` — required by
  `chrome.tabs.captureVisibleTab` (chrome refuses screenshot calls
  without `<all_urls>` or `activeTab`, and `activeTab` only works on
  user-gesture, not SW-driven RPC). Production V2 should evaluate
  `chrome.debugger` (`Page.captureScreenshot`) or an offscreen
  document + `html2canvas` to drop `<all_urls>` and tighten the
  attack surface (design v0.6 §13.2.1).
* No license / token auth — anyone on `127.0.0.1` can connect (spike).
* No tab/window pinning yet — `chrome.tabs.create` opens in whatever chrome
  window is current for that profile. Real workstation tab management
  (design v0.6 §4.6, `(window_id, tab_id)` lock) is the next spike step.

## Layout

```
extension/
├── manifest.json            # MV3, no `key`, no default_locale (spike)
├── package.json
├── vite.config.ts           # multi-entry, copies manifest + popup.html + icons
├── tsconfig.json
├── icons/icon128.png        # placeholder
└── src/
    ├── background.ts        # SW: ws connect, register, alarms, fake task handler
    ├── content/flow_dom.ts  # placeholder (lazy-injected, not auto-match)
    ├── popup/
    │   ├── popup.html
    │   └── popup.ts
    └── lib/
        ├── protocol.ts      # spike message schema
        └── ws_client.ts     # backoff + jitter (C-034)
```

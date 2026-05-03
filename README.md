# Flow Harvester

Local-first Web tool that drives Google Flow (labs.google/fx/tools/flow) to
batch-harvest Veo videos from a managed pool of Google accounts. The
operator drives everything from a browser dashboard at
`http://127.0.0.1:8080/` — no CLI, no YAML editing.

Built around a thin FastAPI server + SQLite + a [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)-driven
worker that re-uses the customer's installed Chrome profile.

---

## Quick start

### Windows (customer install)

1. Install **Google Chrome** and **Python 3.10+** (tick "Add Python to PATH").
2. Drop the project folder somewhere stable (e.g. `D:\FlowHarvester\`).
3. Double-click `setup.bat` once.
4. Double-click `start.bat` whenever you want to use it. Browser opens to the dashboard automatically.

Detailed walk-through: [docs/customer-install-windows.md](docs/customer-install-windows.md).

### macOS / Linux (developer)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
flow-harvester serve --port 8080
```

Open <http://127.0.0.1:8080/>.

---

## What it does

* Manages **3-5 Google accounts** as "workstations" — each with its own
  persistent Chrome profile, daily task limit, and Flow project URL.
* Customer creates tasks via a **Web form** (single) or
  **CSV bulk upload** (many). Each task = SKU + creative + segment +
  prompt + N target videos + reference image(s).
* The scheduler claims (workstation, task) pairs, drives Chrome through
  the Flow UI, downloads each generated mp4 into
  `Documents/FlowHarvester/output/<date>/<sku>/<creative>/segment_<x>/`.
* Per-account **strike-based cooldowns** when Google's `unusual_activity`
  fence trips; auto-recovery via probe; full re-login from the UI when
  things genuinely go sideways.
* Two **operating modes** toggleable from the top nav:
  * ☀️ **日间** (supervised) — 60s stagger, full concurrency, captcha
    pauses task for the operator.
  * 🌙 **夜间** (unattended) — 120s stagger, max 2 concurrent, captcha
    skips the task to manual_review so the queue keeps moving.

---

## Documentation

* [Customer manual (Chinese)](docs/customer-manual.md) — what the
  operator does day-to-day.
* [Windows install guide](docs/customer-install-windows.md) — first-run
  setup on customer Win10/11 machines.
* [Architecture](docs/architecture.md) — daemon / scheduler / worker
  decomposition.
* [Data and storage](docs/data-and-storage.md) — DB schema, output
  layout, retention policy.
* [Workflow and scheduling](docs/workflow-and-scheduling.md) —
  state machines, cooldown tiers, recovery.
* [Troubleshooting](docs/troubleshooting.md) — known Flow quirks +
  fixes (audio failure, stale cards, prompt-attach selectors).
* [Operations and reports](docs/operations-and-reports.md) — daily
  report format, per-WS health.

---

## Stack

| Layer | Pick |
|-------|------|
| Language | Python 3.10+ |
| Browser automation | patchright (anti-detection Playwright fork) |
| Web | FastAPI + uvicorn + Jinja2 + Tailwind CDN + HTMX |
| Live updates | WebSocket (per-dashboard / per-task) |
| Persistence | SQLite (WAL) |
| Bulk import | CSV + multipart image upload |
| Logs | Local log files (`worker_<id>.log`, `scheduler.log`) |

---

## Tests

```bash
.venv/bin/pytest -q
```

262 tests passing (one skipped — needs `patchright install chromium`,
not used in production where customers use the system Chrome).

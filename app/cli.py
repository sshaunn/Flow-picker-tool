"""CLI entry point for flow-harvester.

Subcommands:

* ``import-tasks``  Validate and load ``tasks.csv`` into the database.
* ``run-once``      Run scheduler one round (single workstation, single task).
* ``run-worker``    Multi-workstation scheduler loop (V1).
* ``report``        Generate the daily report for a given date.
* ``init-db``       Create database schema if missing.
* ``check-config``  Validate config files only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from app.config import ConfigError, load_config


def _load_or_die(settings: str, workstations: str):
    try:
        cfg, ws = load_config(settings, workstations)
    except ConfigError as exc:
        click.echo(f"[config error] {exc}", err=True)
        sys.exit(2)
    # Make sure the resolved app-data dirs exist before any subcommand
    # writes to them. Covers both dev (./output, ./logs in repo) and the
    # customer install (%LOCALAPPDATA%\FlowHarvester\... on first run).
    Path(cfg.output_root).mkdir(parents=True, exist_ok=True)
    Path(cfg.log_root).mkdir(parents=True, exist_ok=True)
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    return cfg, ws


@click.group()
@click.option(
    "--settings",
    default="config/settings.yaml",
    show_default=True,
    help="Path to settings.yaml",
)
@click.option(
    "--workstations",
    default="config/workstations.yaml",
    show_default=True,
    help="Path to workstations.yaml",
)
@click.pass_context
def cli(ctx: click.Context, settings: str, workstations: str) -> None:
    """Flow Harvester CLI."""
    ctx.ensure_object(dict)
    ctx.obj["settings_path"] = settings
    ctx.obj["workstations_path"] = workstations


@cli.command("check-config")
@click.pass_context
def check_config(ctx: click.Context) -> None:
    """Load and print resolved configuration."""
    cfg, ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    click.echo("settings: OK")
    click.echo(f"  flow.entry_url={cfg.flow.entry_url}")
    click.echo(f"  generation.max_round_per_task={cfg.generation.max_round_per_task}")
    click.echo(f"  generation.max_retry_count={cfg.generation.max_retry_count}")
    click.echo(f"  cooldown.consecutive_failure_threshold={cfg.cooldown.consecutive_failure_threshold}")
    click.echo(f"workstations: {len(ws)}")
    for w in ws:
        click.echo(f"  - {w.id} status={w.status} daily_limit={w.daily_task_limit}")


@cli.command("init-db")
@click.pass_context
def init_db(ctx: click.Context) -> None:
    """Create the database schema (idempotent)."""
    from app.db.schema import init_schema

    cfg, _ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    db_path = Path(cfg.db_path)
    init_schema(db_path)
    click.echo(f"db ready: {db_path}")


@cli.command("import-tasks")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def import_tasks(ctx: click.Context, csv_path: Path) -> None:
    """Validate and import tasks from a CSV file."""
    from app.db.schema import init_schema
    from app.tasks.importer import import_tasks as do_import, ImportError as TaskImportError

    cfg, _ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    db_path = Path(cfg.db_path)
    init_schema(db_path)
    try:
        result = do_import(csv_path, db_path, default_max_retry=cfg.generation.max_retry_count)
    except TaskImportError as exc:
        click.echo(f"[import error] {exc}", err=True)
        sys.exit(3)
    click.echo(f"imported {result.inserted} task(s); skipped {result.skipped}")


@cli.command("sync-workstations")
@click.pass_context
def sync_workstations(ctx: click.Context) -> None:
    """Sync workstations.yaml into the database."""
    from app.db.schema import init_schema
    from app.workstations.sync import sync_workstations as do_sync

    cfg, ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    db_path = Path(cfg.db_path)
    init_schema(db_path)
    inserted, updated = do_sync(db_path, ws)
    click.echo(f"workstations: inserted={inserted} updated={updated}")


@cli.command("run-once")
@click.option("--workstation", "ws_id", required=False, help="Workstation id (V0 single-station mode)")
@click.option("--max-tasks", default=1, show_default=True, help="Max tasks to execute this run")
@click.option("--mock/--no-mock", default=False, help="Use a mock Flow page (no Playwright)")
@click.pass_context
def run_once(ctx: click.Context, ws_id: str | None, max_tasks: int, mock: bool) -> None:
    """Pick up and execute up to N task(s) on a single workstation (V0 path)."""
    from app.runner.single import run_single_workstation

    cfg, ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    db_path = Path(cfg.db_path)
    summary = run_single_workstation(
        db_path=db_path,
        config=cfg,
        workstations=ws,
        target_workstation_id=ws_id,
        max_tasks=max_tasks,
        use_mock=mock,
    )
    click.echo(
        f"executed={summary.executed} success={summary.success} "
        f"failed={summary.failed} download_failed={summary.download_failed} "
        f"retry_waiting={summary.retry_waiting}"
    )


@cli.command("run-worker")
@click.option("--max-rounds", default=0, help="Max scheduling rounds (0 = run until queue is empty)")
@click.option("--mock/--no-mock", default=False, help="Use a mock Flow page (no Playwright)")
@click.pass_context
def run_worker(ctx: click.Context, max_rounds: int, mock: bool) -> None:
    """Run the multi-workstation scheduler loop (V1)."""
    from app.runner.multi import run_multi_workstation

    cfg, ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    db_path = Path(cfg.db_path)
    summary = run_multi_workstation(
        db_path=db_path,
        config=cfg,
        workstations=ws,
        max_rounds=max_rounds,
        use_mock=mock,
    )
    click.echo(
        f"executed={summary.executed} success={summary.success} "
        f"failed={summary.failed} download_failed={summary.download_failed} "
        f"retry_waiting={summary.retry_waiting}"
    )


@cli.command("login-flow")
@click.option("--workstation", "ws_id", required=False, help="Workstation id")
@click.option("--all", "do_all", is_flag=True, help="Walk through every workstation in sequence")
@click.option(
    "--probe/--no-probe",
    default=None,
    help="Probe each profile first and skip ones already signed in. "
         "Default: on for --all, off for single --workstation (first-time login is wasted otherwise).",
)
@click.pass_context
def login_flow(ctx: click.Context, ws_id: str | None, do_all: bool, probe: bool | None) -> None:
    """Open Flow inside a workstation profile so a human can sign in once.

    Modes:

    * ``--workstation WS_X``: open one profile, wait for ENTER, close.
      No probe by default (first-time login would just waste time).
    * ``--all``: iterate every workstation. Probes each profile silently
      and skips ones already signed in.

    Cookies persist in the profile directory; you only do this once per
    account unless Flow ages them out (typically weeks).
    """
    from app.worker.flow_playwright import open_login_helper
    from app.worker.login_status import LoginStatus, probe_workstation

    cfg, ws_list = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])

    if not do_all:
        if ws_id is None:
            click.echo("[error] either --workstation or --all is required", err=True)
            sys.exit(2)
        targets = [w for w in ws_list if w.id == ws_id]
        if not targets:
            click.echo(f"[error] workstation not found: {ws_id}", err=True)
            sys.exit(2)
    else:
        targets = list(ws_list)

    # Probe defaults: on for --all (skip already-logged-in), off for single
    # --workstation (the operator usually wants to log in immediately).
    if probe is None:
        probe = do_all

    for idx, target in enumerate(targets, start=1):
        profile = Path(target.browser_profile_path)
        profile.mkdir(parents=True, exist_ok=True)

        if probe:
            click.echo(f"[{idx}/{len(targets)}] probing {target.id} ({profile})...")
            result = probe_workstation(
                workstation_id=target.id,
                profile_path=profile,
                entry_url=cfg.flow.entry_url,
                project_url=target.flow_project_url,
                headless=True,
                timeout_sec=30,
            )
            if result.status == LoginStatus.LOGGED_IN:
                click.echo(f"  [skip] {target.id}: already signed in ({result.detail})")
                continue
            if result.status == LoginStatus.NEEDS_MANUAL_CHECK:
                click.echo(
                    f"  [warn] {target.id}: {result.detail} — opening for manual check"
                )

        click.echo(f"[{idx}/{len(targets)}] opening Flow for {target.id} ({profile})")
        click.echo("       sign in to Google / Flow in the opened window, then press ENTER here.")
        with open_login_helper(profile_path=profile, entry_url=cfg.flow.entry_url):
            try:
                click.prompt(f"  press ENTER when {target.id} is done",
                              default="", show_default=False)
            except click.Abort:
                click.echo("aborted")
                return
    click.echo("login pass complete. you can now run-once / run-worker.")


@cli.command("check-login")
@click.option("--workstation", "ws_id", required=False,
              help="Probe a single workstation; default: all workstations")
@click.option("--headless/--no-headless", default=True,
              help="Run probe in a headless browser (default: yes)")
@click.pass_context
def check_login(ctx: click.Context, ws_id: str | None, headless: bool) -> None:
    """Probe each workstation profile and report login status.

    This is *read-only*: it never types a password and never auto-dismisses
    captcha. Use the result to decide which profiles need ``login-flow``.
    """
    from app.worker.login_status import LoginStatus, probe_workstation
    from app.worker.flow_selectors import load_flow_selectors

    cfg, ws_list = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    selectors_cfg = load_flow_selectors()

    if ws_id is not None:
        ws_list = [w for w in ws_list if w.id == ws_id]
        if not ws_list:
            click.echo(f"[error] workstation not found: {ws_id}", err=True)
            sys.exit(2)

    needs_action: list[str] = []
    for target in ws_list:
        profile = Path(target.browser_profile_path)
        probe = probe_workstation(
            workstation_id=target.id,
            profile_path=profile,
            entry_url=cfg.flow.entry_url,
            project_url=target.flow_project_url,
            selectors_cfg=selectors_cfg,
            headless=headless,
            timeout_sec=30,
        )
        marker = {
            LoginStatus.LOGGED_IN: "[OK ]",
            LoginStatus.NEEDS_LOGIN: "[NEW]",
            LoginStatus.NEEDS_MANUAL_CHECK: "[!! ]",
            LoginStatus.PROFILE_MISSING: "[??]",
            LoginStatus.PROBE_FAILED: "[ER ]",
        }[probe.status]
        click.echo(f"  {marker} {target.id}: {probe.status.value} — {probe.detail}")
        if probe.status != LoginStatus.LOGGED_IN:
            needs_action.append(target.id)

    if needs_action:
        click.echo("")
        click.echo(f"profiles needing attention: {', '.join(needs_action)}")
        click.echo(f"run: flow-harvester login-flow --all   # interactively walk them")


@cli.command("discover-selectors")
@click.option("--workstation", "ws_id", required=True, help="Workstation id")
@click.option(
    "--screenshot",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="If set, save a full-page screenshot of the loaded page",
)
@click.pass_context
def discover_selectors(ctx: click.Context, ws_id: str, screenshot: Path | None) -> None:
    """Open Flow once and report which configured selectors are visible.

    Use this after login-flow to validate ``config/flow-selectors.yaml``
    against the live Flow DOM. Prints a checklist and (optionally) saves a
    screenshot for offline DOM inspection.
    """
    from app.worker.flow_playwright import open_login_helper
    from app.worker.flow_selectors import load_flow_selectors

    cfg, ws_list = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    target = next((w for w in ws_list if w.id == ws_id), None)
    if target is None:
        click.echo(f"[error] workstation not found: {ws_id}", err=True)
        sys.exit(2)

    selectors_cfg = load_flow_selectors()
    sel = selectors_cfg.selectors

    profile = Path(target.browser_profile_path)
    # Prefer the per-workstation project URL (where the prompt editor lives);
    # the tools home page is just a project picker so selectors won't match.
    open_url = target.flow_project_url or cfg.flow.entry_url
    click.echo(f"opening Flow with profile: {profile}")
    click.echo(f"               url: {open_url}")
    with open_login_helper(profile_path=profile, entry_url=open_url) as page:
        page.wait_for_load_state("domcontentloaded")
        for label, css in [
            ("upload_button", sel.upload_button),
            ("prompt_input", sel.prompt_input),
            ("generate_button", sel.generate_button),
            ("generation_complete_marker", sel.generation_complete_marker),
            ("candidate_items", sel.candidate_items),
            ("candidate_download_button", sel.candidate_download_button),
        ]:
            try:
                count = page.locator(css).count()
            except Exception as exc:  # noqa: BLE001
                count = -1
                click.echo(f"  [error] {label}={css!r}: {exc}", err=True)
                continue
            tag = "[OK ]" if count > 0 else "[ -- ]"
            click.echo(f"  {tag} {label}: count={count}  selector={css}")

        if screenshot is not None:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot), full_page=True)
            click.echo(f"screenshot: {screenshot}")
        click.echo("==> press ENTER here to close the browser when you're done inspecting.")
        try:
            click.prompt("", default="", show_default=False)
        except click.Abort:
            pass


@cli.command("recover-banned")
@click.option(
    "--workstation", "ws_id",
    default=None,
    help="Probe only this workstation (default: all manual_check WSs whose "
         "cooldown_until has elapsed)",
)
@click.option("--headless/--no-headless", default=True, show_default=True,
              help="Headless for cron; --no-headless to watch the probe")
@click.pass_context
def recover_banned(ctx: click.Context, ws_id: str | None, headless: bool) -> None:
    """Probe-based recovery for manual_check workstations.

    For each ``manual_check`` workstation whose ``cooldown_until`` has
    elapsed, opens its Flow project URL with the persisted profile,
    checks for unusual_activity / captcha phrases, and either:

    * flips the WS back to ``healthy`` (clean probe) and resets the probe
      counter; or
    * advances to the next ``unusual_activity_probe_backoff_hours`` tier
      (still banned); or
    * after the last tier, drops ``cooldown_until`` so the WS stays in
      manual_check until an operator intervenes.

    Cron friendly — designed to run alongside ``run-worker`` on an hourly
    schedule. The probe never clicks Create / never burns a generation
    quota; it's a read-only page load.
    """
    from app.db.connection import connect, transaction
    from app.scheduler.state import probe_recover_banned_workstations
    from app.worker.flow_selectors import load_flow_selectors
    from app.worker.login_status import LoginStatus, probe_workstation

    cfg, ws_list = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    selectors_cfg = load_flow_selectors()
    ws_by_id = {w.id: w for w in ws_list}

    def probe_fn(workstation_id: str) -> bool:
        ws = ws_by_id.get(workstation_id)
        if ws is None:
            click.echo(f"[warn] WS {workstation_id} in DB but not in yaml; "
                       "skipping probe", err=True)
            return True  # treat as still banned, conservative
        result = probe_workstation(
            workstation_id=workstation_id,
            profile_path=Path(ws.browser_profile_path),
            entry_url=cfg.flow.entry_url,
            project_url=ws.flow_project_url,
            selectors_cfg=selectors_cfg,
            headless=headless,
            timeout_sec=30,
        )
        click.echo(f"  probe {workstation_id}: {result.status.value} "
                   f"({result.detail})")
        return result.status == LoginStatus.NEEDS_MANUAL_CHECK

    with connect(cfg.db_path) as conn:
        if ws_id is not None:
            row = conn.execute(
                "SELECT id, status, cooldown_until, ban_probe_count "
                "FROM workstations WHERE id = ?", (ws_id,)
            ).fetchone()
            if row is None:
                click.echo(f"[error] workstation not found: {ws_id}", err=True)
                sys.exit(2)
            if row["status"] != "manual_check":
                click.echo(f"[skip] {ws_id} is {row['status']}, not manual_check")
                return
            click.echo(f"probing {ws_id} (probe_count={row['ban_probe_count']})")
            still_banned = probe_fn(ws_id)
            with transaction(conn):
                # Force cooldown_until to past so the recovery function
                # picks this WS up regardless of its current schedule.
                conn.execute(
                    "UPDATE workstations SET cooldown_until = datetime('now', '-1 second') "
                    "WHERE id = ? AND status = 'manual_check'", (ws_id,)
                )
                stats = probe_recover_banned_workstations(
                    conn, cooldown_cfg=cfg.cooldown,
                    probe_fn=lambda _: still_banned,
                )
        else:
            with transaction(conn):
                stats = probe_recover_banned_workstations(
                    conn, cooldown_cfg=cfg.cooldown, probe_fn=probe_fn,
                )
    click.echo(f"recovered={stats['recovered']} "
               f"still_banned={stats['still_banned']} "
               f"exhausted={stats['exhausted']}")


@cli.command("dump-mode-panel")
@click.option("--workstation", "ws_id", required=True, help="Workstation id")
@click.option(
    "--url",
    default=None,
    help="Override Flow URL to open (default: workstation's flow_project_url)",
)
@click.option(
    "--screenshot",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Optional full-page screenshot path",
)
@click.option(
    "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write enumeration JSON to this file (default: stdout)",
)
@click.pass_context
def dump_mode_panel(
    ctx: click.Context,
    ws_id: str,
    url: str | None,
    screenshot: Path | None,
    out: Path | None,
) -> None:
    """Dump every ``button[data-state]`` on a Flow project page so we can
    refine ``mode_controls.settings_trigger`` in flow-selectors.yaml.

    Use this on a *fresh* project (no manual pre-run) to discover what the
    settings summary button actually looks like. Output: text, aria-label,
    visibility, classes, attribute set — enough to write a unique selector.
    """
    import json
    from app.worker.flow_playwright import open_login_helper

    cfg, ws_list = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    target = next((w for w in ws_list if w.id == ws_id), None)
    if target is None:
        click.echo(f"[error] workstation not found: {ws_id}", err=True)
        sys.exit(2)

    profile = Path(target.browser_profile_path)
    open_url = url or target.flow_project_url or cfg.flow.entry_url
    click.echo(f"opening Flow with profile: {profile}")
    click.echo(f"               url: {open_url}")
    with open_login_helper(profile_path=profile, entry_url=open_url) as page:
        page.wait_for_load_state("domcontentloaded")
        # Wait until the SPA has actually rendered something — Flow shows
        # a "Loading..." splash for ~6-12s on a fresh project before any
        # button mounts. Poll up to 30s for the prompt textarea OR any
        # button[data-state] to appear, whichever comes first.
        deadline_ms = 30_000
        try:
            page.wait_for_function(
                """() => {
                    if (document.body.innerText.trim() === 'Loading...') return false;
                    return document.querySelector('button[data-state]')
                        || document.querySelector('[role="textbox"][data-slate-editor="true"]')
                        || document.querySelector('[role="button"]');
                }""",
                timeout=deadline_ms,
            )
            click.echo("page hydrated; capturing DOM ...")
        except Exception as exc:  # noqa: BLE001
            click.echo(f"[warn] hydration timeout: {exc}", err=True)
        # A small additional settle so lazy-mounted settings trigger lands.
        page.wait_for_timeout(1_500)
        info = page.evaluate(
            """
            () => {
                const dump = (b) => ({
                    tag: b.tagName,
                    role: b.getAttribute('role'),
                    state: b.getAttribute('data-state'),
                    aria_label: b.getAttribute('aria-label'),
                    aria_haspopup: b.getAttribute('aria-haspopup'),
                    aria_expanded: b.getAttribute('aria-expanded'),
                    id_suffix: (b.id || '').replace(/^radix-:/, ':'),
                    classes: (b.className || '').toString().split(/\\s+/).slice(0, 6),
                    text: ((b.innerText || b.textContent || '').trim()
                              .replace(/\\s+/g, ' ').slice(0, 200)),
                    visible: !!(b.offsetWidth || b.offsetHeight),
                    rect: (() => {
                        const r = b.getBoundingClientRect();
                        return {x: Math.round(r.x), y: Math.round(r.y),
                                w: Math.round(r.width), h: Math.round(r.height)};
                    })(),
                });
                return {
                    url: location.href,
                    title: document.title,
                    data_state_buttons: [...document.querySelectorAll('button[data-state]')]
                        .slice(0, 60).map(dump),
                    role_button: [...document.querySelectorAll('[role="button"]')]
                        .slice(0, 30).map(dump),
                    role_tab: [...document.querySelectorAll('[role="tab"]')]
                        .slice(0, 30).map(dump),
                };
            }
            """
        ) or {}
        text = json.dumps(info, indent=2, ensure_ascii=False)
        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            click.echo(f"wrote: {out}")
        else:
            click.echo(text)
        if screenshot is not None:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot), full_page=True)
            click.echo(f"screenshot: {screenshot}")
        click.echo("==> press ENTER to close.")
        try:
            click.prompt("", default="", show_default=False)
        except click.Abort:
            pass


@cli.command("task-reset")
@click.argument("task_id")
@click.option(
    "--full",
    is_flag=True,
    help="Also clear downloaded_count, generation_round_count, and delete "
         "task_results rows for this task. Files on disk are NOT touched "
         "(see docs/data-and-storage.md: ``task_results`` rows + files "
         "are persisted by design). Use only when iterating during testing.",
)
@click.pass_context
def task_reset(ctx: click.Context, task_id: str, full: bool) -> None:
    """Reset a task so it can be re-claimed by the scheduler.

    Default reset (use during normal operation):
      * status -> 'pending'
      * retry_count -> 0
      * assigned_workstation_id, error_type, error_message,
        started_at, finished_at -> NULL

    With ``--full`` (testing only): also resets ``downloaded_count`` and
    ``generation_round_count`` to 0 and removes the matching
    ``task_results`` rows from the DB. The actual video/image files on
    disk are preserved.
    """
    import sqlite3

    from app.db.connection import connect, transaction

    cfg, _ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    conn = connect(cfg.db_path)
    try:
        with transaction(conn):
            row = conn.execute(
                "SELECT status, retry_count, downloaded_count, generation_round_count "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                click.echo(f"[error] task not found: {task_id}", err=True)
                sys.exit(2)

            click.echo(
                f"before: status={row['status']} retry={row['retry_count']} "
                f"downloaded={row['downloaded_count']} round={row['generation_round_count']}"
            )

            if full:
                conn.execute(
                    """
                    UPDATE tasks SET
                        status = 'pending',
                        retry_count = 0,
                        assigned_workstation_id = NULL,
                        error_type = NULL,
                        error_message = NULL,
                        started_at = NULL,
                        finished_at = NULL,
                        downloaded_count = 0,
                        generation_round_count = 0,
                        zombie_recovery_count = 0
                    WHERE task_id = ?
                    """,
                    (task_id,),
                )
                deleted = conn.execute(
                    "DELETE FROM task_results WHERE task_id = ?", (task_id,)
                ).rowcount
                click.echo(f"  cleared {deleted} task_results row(s)")
            else:
                conn.execute(
                    """
                    UPDATE tasks SET
                        status = 'pending',
                        retry_count = 0,
                        assigned_workstation_id = NULL,
                        error_type = NULL,
                        error_message = NULL,
                        started_at = NULL,
                        finished_at = NULL
                    WHERE task_id = ?
                    """,
                    (task_id,),
                )

            after = conn.execute(
                "SELECT status, retry_count, downloaded_count, generation_round_count "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            click.echo(
                f"after:  status={after['status']} retry={after['retry_count']} "
                f"downloaded={after['downloaded_count']} round={after['generation_round_count']}"
            )
    finally:
        conn.close()


@cli.command("report")
@click.option("--date", "report_date", required=False, help="Report date YYYY-MM-DD (default: today)")
@click.pass_context
def report(ctx: click.Context, report_date: str | None) -> None:
    """Generate the daily report markdown."""
    from app.reports.daily import generate_daily_report

    cfg, _ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    db_path = Path(cfg.db_path)
    out_path = generate_daily_report(
        db_path=db_path,
        output_root=Path(cfg.output_root),
        report_date=report_date,
    )
    click.echo(f"report written: {out_path}")


@cli.group("workstation")
def workstation_group() -> None:
    """Manage workstation records in the DB.

    The Web UI calls into the same repository; this CLI group exists so the
    customer install can be smoke-tested from a terminal, and so dev
    iterations don't have to round-trip through yaml.
    """


@workstation_group.command("list")
@click.pass_context
def workstation_list(ctx: click.Context) -> None:
    """List workstation records currently in the DB."""
    from app.db.connection import connect
    from app.db.schema import init_schema
    from app.workstations.repository import list_workstations

    cfg, _ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    init_schema(Path(cfg.db_path))
    with connect(cfg.db_path) as conn:
        rows = list_workstations(conn)
    if not rows:
        click.echo("(no workstations)")
        return
    for ws in rows:
        click.echo(
            f"  {ws.id} status={ws.status} daily_limit={ws.daily_task_limit} "
            f"profile={ws.browser_profile_path} project={ws.flow_project_url or '-'}"
        )


@workstation_group.command("add")
@click.option("--id", "ws_id", required=True, help="Workstation id, e.g. WS_A")
@click.option("--account", "account_label", required=True, help="Account label")
@click.option("--profile", "profile_path", default=None,
              help="Browser profile dir (default: paths.workstation_profile_path(id))")
@click.option("--daily-limit", "daily_limit", default=20, show_default=True,
              type=int, help="Daily task limit")
@click.option("--project-url", "project_url", default=None,
              help="Flow project URL (paste after first login)")
@click.option("--mode-tab", default=None, help="video | image")
@click.option("--mode-subtab", default=None, help="ingredients | frames")
@click.option("--mode-aspect", default=None, help="9:16 | 16:9 | 1:1")
@click.option("--mode-output-count", default=None, type=int, help="1..4")
@click.option("--mode-duration-sec", default=None, type=int, help="4 | 6 | 8")
@click.option("--mode-model", default=None, help="e.g. 'Veo 3.1 - Fast'")
@click.pass_context
def workstation_add(
    ctx: click.Context,
    ws_id: str,
    account_label: str,
    profile_path: str | None,
    daily_limit: int,
    project_url: str | None,
    mode_tab: str | None,
    mode_subtab: str | None,
    mode_aspect: str | None,
    mode_output_count: int | None,
    mode_duration_sec: int | None,
    mode_model: str | None,
) -> None:
    """Add a new workstation row (id must not already exist)."""
    from app import paths as app_paths
    from app.config.loader import FlowModeSpec, WorkstationConfig
    from app.db.connection import connect
    from app.db.schema import init_schema
    from app.workstations.repository import (
        WorkstationConflictError,
        create_workstation,
    )

    cfg, _ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    init_schema(Path(cfg.db_path))

    if profile_path is None:
        profile_path = str(app_paths.workstation_profile_path(ws_id))

    mode_kwargs = {
        "tab": mode_tab,
        "subtab": mode_subtab,
        "aspect": mode_aspect,
        "output_count": mode_output_count,
        "duration_sec": mode_duration_sec,
        "model": mode_model,
    }
    flow_mode = (
        FlowModeSpec(**mode_kwargs)
        if any(v is not None for v in mode_kwargs.values())
        else None
    )

    try:
        ws = WorkstationConfig(
            id=ws_id,
            account_label=account_label,
            browser_profile_path=profile_path,
            daily_task_limit=daily_limit,
            flow_project_url=project_url,
            flow_mode=flow_mode,
        )
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError, etc.
        click.echo(f"[validation error] {exc}", err=True)
        sys.exit(2)

    with connect(cfg.db_path) as conn:
        try:
            create_workstation(conn, ws)
        except WorkstationConflictError as exc:
            click.echo(f"[error] {exc}", err=True)
            sys.exit(2)
    Path(profile_path).mkdir(parents=True, exist_ok=True)
    click.echo(f"added: {ws_id}  profile={profile_path}")


@workstation_group.command("update")
@click.option("--id", "ws_id", required=True, help="Workstation id to update")
@click.option("--account", "account_label", default=None)
@click.option("--profile", "profile_path", default=None)
@click.option("--daily-limit", "daily_limit", default=None, type=int)
@click.option("--project-url", "project_url", default=None,
              help="Use 'CLEAR' to set to NULL")
@click.pass_context
def workstation_update(
    ctx: click.Context,
    ws_id: str,
    account_label: str | None,
    profile_path: str | None,
    daily_limit: int | None,
    project_url: str | None,
) -> None:
    """Patch one or more editable fields on an existing workstation."""
    from app.db.connection import connect
    from app.db.schema import init_schema
    from app.workstations.repository import (
        WorkstationNotFoundError,
        update_workstation_config,
    )

    cfg, _ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    init_schema(Path(cfg.db_path))

    fields: dict = {}
    if account_label is not None:
        fields["account_label"] = account_label
    if profile_path is not None:
        fields["browser_profile_path"] = profile_path
    if daily_limit is not None:
        fields["daily_task_limit"] = daily_limit
    if project_url is not None:
        fields["flow_project_url"] = None if project_url == "CLEAR" else project_url
    if not fields:
        click.echo("[error] specify at least one field to update", err=True)
        sys.exit(2)

    with connect(cfg.db_path) as conn:
        try:
            update_workstation_config(conn, ws_id, **fields)
        except WorkstationNotFoundError:
            click.echo(f"[error] workstation not found: {ws_id}", err=True)
            sys.exit(2)
    click.echo(f"updated: {ws_id}  fields={list(fields)}")


@workstation_group.command("delete")
@click.option("--id", "ws_id", required=True, help="Workstation id to delete")
@click.option("--wipe-profile", is_flag=True,
              help="Also delete the Chrome profile dir on disk so the next "
                   "login starts from scratch. Only wipes paths under the "
                   "managed profiles dir; custom paths are left alone.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def workstation_delete(ctx: click.Context, ws_id: str, wipe_profile: bool, yes: bool) -> None:
    """Remove a workstation row. Use carefully — task history that references
    this id will become orphaned (the FK is informational, not enforced)."""
    from app.db.connection import connect
    from app.db.schema import init_schema
    from app.workstations.repository import delete_workstation

    cfg, _ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    init_schema(Path(cfg.db_path))

    if not yes:
        prompt = f"delete workstation {ws_id}"
        if wipe_profile:
            prompt += " and wipe its Chrome profile dir"
        click.confirm(prompt + "?", abort=True)
    with connect(cfg.db_path) as conn:
        ok = delete_workstation(conn, ws_id, wipe_profile=wipe_profile)
    if not ok:
        click.echo(f"[error] workstation not found: {ws_id}", err=True)
        sys.exit(2)
    click.echo(f"deleted: {ws_id}" + (" (profile wiped)" if wipe_profile else ""))


@cli.group("task")
def task_group() -> None:
    """Manage task records via the form-style API.

    The Web UI's "New task" form posts into ``app.tasks.repository`` and
    these subcommands hit the same code path so the customer install can
    be smoke-tested without the GUI.
    """


@task_group.command("add")
@click.option("--sku", "sku_id", required=True)
@click.option("--creative", "creative_id", required=True)
@click.option("--segment", "segment_id", required=True)
@click.option("--prompt", "video_prompt", required=True,
              help="Prompt text (use shell quoting for multi-word)")
@click.option("--target", "target_count", default=4, show_default=True, type=int,
              help="How many videos to generate")
@click.option("--asset", "assets", multiple=True, required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Source image path (repeat for multiple assets)")
@click.option("--asset-kind", "asset_kind", default="reference", show_default=True,
              help="first_frame | last_frame | previous_segment_frame | reference | other")
@click.option("--no-copy", is_flag=True,
              help="Don't copy uploads into managed assets dir (use original paths)")
@click.option("--task-id", "task_id", default=None,
              help="Override auto-generated task id")
@click.option("--depends-on", "depends_on", default=None)
@click.option("--max-retry", "max_retry", default=None, type=int)
@click.pass_context
def task_add(
    ctx: click.Context,
    sku_id: str,
    creative_id: str,
    segment_id: str,
    video_prompt: str,
    target_count: int,
    assets: tuple[Path, ...],
    asset_kind: str,
    no_copy: bool,
    task_id: str | None,
    depends_on: str | None,
    max_retry: int | None,
) -> None:
    """Create one task with one or more source images via the form API."""
    from app.db.connection import connect
    from app.db.schema import init_schema
    from app.tasks.repository import (
        AssetDraft, TaskDraft, TaskRepositoryError, create_task,
    )

    cfg, _ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    init_schema(Path(cfg.db_path))

    asset_drafts = [
        AssetDraft(path=p, kind=asset_kind, copy_into_managed_dir=not no_copy)
        for p in assets
    ]
    draft = TaskDraft(
        sku_id=sku_id,
        creative_id=creative_id,
        segment_id=segment_id,
        video_prompt=video_prompt,
        target_count=target_count,
        assets=asset_drafts,
        depends_on_task_id=depends_on,
        max_retry_count=max_retry,
        task_id=task_id,
    )
    with connect(cfg.db_path) as conn:
        try:
            new_id = create_task(
                conn, draft,
                default_max_retry=cfg.generation.max_retry_count,
            )
        except TaskRepositoryError as exc:
            click.echo(f"[error] {exc}", err=True)
            sys.exit(2)
    click.echo(f"created: {new_id}")


@task_group.command("list")
@click.option("--status", default=None,
              help="Filter by status: pending | running | success | retry_waiting | "
                   "failed | download_failed | manual_review")
@click.option("--limit", default=20, show_default=True, type=int)
@click.pass_context
def task_list(ctx: click.Context, status: str | None, limit: int) -> None:
    """List recent tasks, newest first."""
    from app.db.connection import connect
    from app.db.schema import init_schema
    from app.tasks.repository import list_tasks

    cfg, _ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    init_schema(Path(cfg.db_path))
    with connect(cfg.db_path) as conn:
        rows = list_tasks(conn, status=status, limit=limit)
    if not rows:
        click.echo("(no tasks)")
        return
    for t in rows:
        click.echo(
            f"  {t.task_id}  status={t.status}  "
            f"{t.downloaded_count}/{t.target_count}  "
            f"sku={t.sku_id}  segment={t.creative_id}/{t.segment_id}  "
            f"prompt={t.video_prompt[:40] + ('...' if len(t.video_prompt) > 40 else '')!r}"
        )


@task_group.command("show")
@click.argument("task_id")
@click.pass_context
def task_show(ctx: click.Context, task_id: str) -> None:
    """Show full details for one task, including its source assets."""
    from app.db.connection import connect
    from app.db.schema import init_schema
    from app.tasks.repository import get_task, get_task_assets

    cfg, _ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    init_schema(Path(cfg.db_path))
    with connect(cfg.db_path) as conn:
        record = get_task(conn, task_id)
        if record is None:
            click.echo(f"[error] task not found: {task_id}", err=True)
            sys.exit(2)
        assets = get_task_assets(conn, task_id)
    click.echo(f"task_id        : {record.task_id}")
    click.echo(f"status         : {record.status}")
    click.echo(f"sku/creative/seg: {record.sku_id} / {record.creative_id} / {record.segment_id}")
    click.echo(f"target / done  : {record.target_count} / {record.downloaded_count}")
    click.echo(f"round / retry  : {record.generation_round_count} / {record.retry_count} (max {record.max_retry_count})")
    click.echo(f"assigned ws    : {record.assigned_workstation_id or '-'}")
    click.echo(f"depends_on     : {record.depends_on_task_id or '-'}")
    click.echo(f"prompt         : {record.video_prompt}")
    if record.error_type:
        click.echo(f"error          : [{record.error_type}] {record.error_message or ''}")
    click.echo(f"created_at     : {record.created_at}")
    click.echo("assets:")
    for order, path, kind in assets:
        click.echo(f"  {order:02d}  [{kind}] {path}")


@task_group.command("delete")
@click.argument("task_id")
@click.option("--force", is_flag=True, help="Allow deleting a running task")
@click.option("--keep-assets", is_flag=True, help="Don't remove the assets dir on disk")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def task_delete(
    ctx: click.Context,
    task_id: str,
    force: bool,
    keep_assets: bool,
    yes: bool,
) -> None:
    """Delete a task row + its task_assets / task_results rows.

    Output mp4 / report files in ``output_root`` are NOT removed (existing
    data contract: persisted videos survive task-row deletion).
    """
    from app.db.connection import connect
    from app.db.schema import init_schema
    from app.tasks.repository import TaskRepositoryError, delete_task

    cfg, _ws = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    init_schema(Path(cfg.db_path))

    if not yes:
        click.confirm(f"delete task {task_id}?", abort=True)
    with connect(cfg.db_path) as conn:
        try:
            ok = delete_task(
                conn, task_id, force=force, remove_assets=not keep_assets,
            )
        except TaskRepositoryError as exc:
            click.echo(f"[error] {exc}", err=True)
            sys.exit(2)
    if not ok:
        click.echo(f"[error] task not found: {task_id}", err=True)
        sys.exit(2)
    click.echo(f"deleted: {task_id}")


@cli.command("scheduler-daemon")
@click.option("--idle-poll-sec", default=5.0, show_default=True, type=float,
              help="Seconds to idle between empty-queue passes")
@click.option("--mock/--no-mock", default=False,
              help="Use mock FlowPort instead of patchright (testing only)")
@click.pass_context
def scheduler_daemon(ctx: click.Context, idle_poll_sec: float, mock: bool) -> None:
    """Run the background scheduler daemon in the foreground (Ctrl-C to stop).

    Useful for terminal smoke tests / debugging the same loop the FastAPI
    server runs on a thread. The customer never invokes this directly —
    their start.bat boots the Web server, which spawns the daemon
    internally.
    """
    import signal
    import time as _time

    from app.db.connection import connect
    from app.db.schema import init_schema
    from app.scheduler.daemon import SchedulerDaemon
    from app.scheduler.recovery import reset_zombie_state_on_startup
    from app.workstations.repository import list_workstations
    from app.workstations.sync import sync_workstations as do_sync

    cfg, ws_yaml = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    init_schema(Path(cfg.db_path))
    # Bootstrap from yaml if needed (DB-as-source-of-truth, but yaml is
    # still the dev convenience path).
    do_sync(cfg.db_path, ws_yaml)
    # Process-boot cleanup: clear any orphaned ``running`` tasks /
    # ``busy`` workstations left behind by the previous process. See
    # reset_zombie_state_on_startup for why this can't wait for the
    # mid-loop recover_zombie_tasks (30-min threshold).
    with connect(cfg.db_path) as conn:
        summary = reset_zombie_state_on_startup(conn)
    if summary.revived or summary.escalated_manual:
        click.echo(
            f"startup zombie cleanup: revived={summary.revived} "
            f"escalated_manual={summary.escalated_manual}"
        )
    with connect(cfg.db_path) as conn:
        ws_list = list_workstations(conn)
    if not ws_list:
        click.echo("[error] no workstations configured in DB", err=True)
        sys.exit(2)

    daemon = SchedulerDaemon(
        db_path=cfg.db_path,
        config=cfg,
        workstations=ws_list,
        idle_poll_sec=idle_poll_sec,
        use_mock=mock,
    )

    def _handle_sigint(signum, frame):  # noqa: ARG001
        click.echo("\n[ctrl-c] stopping daemon...")
        daemon.stop(timeout=60.0)

    signal.signal(signal.SIGINT, _handle_sigint)
    daemon.start()
    click.echo(f"daemon running with {len(ws_list)} workstation(s); "
               f"idle_poll={idle_poll_sec}s. Ctrl-C to stop.")
    try:
        while daemon.is_running:
            _time.sleep(0.5)
    finally:
        daemon.stop(timeout=10.0)
    click.echo("daemon stopped")
    snap = daemon.status()
    click.echo(
        f"summary: rounds={snap.rounds_completed} "
        f"executed={snap.cumulative.executed} "
        f"success={snap.cumulative.success} "
        f"failed={snap.cumulative.failed}"
    )


@cli.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Bind host (use 0.0.0.0 for LAN, 127.0.0.1 for localhost only)")
@click.option("--port", default=8080, show_default=True, type=int)
@click.option("--reload", is_flag=True, help="Auto-reload on code change (dev only)")
@click.option("--no-auto-start", is_flag=True,
              help="Don't auto-start the scheduler daemon at boot")
@click.option("--idle-poll-sec", default=5.0, show_default=True, type=float,
              help="Seconds the daemon idles between empty-queue passes")
@click.pass_context
def serve(
    ctx: click.Context,
    host: str,
    port: int,
    reload: bool,
    no_auto_start: bool,
    idle_poll_sec: float,
) -> None:
    """Run the FastAPI Web UI server.

    The customer's start.bat runs this. The browser hits
    ``http://localhost:8080/`` to reach the dashboard.
    """
    import uvicorn

    from app.db.schema import init_schema
    from app.workstations.sync import sync_workstations as do_sync

    cfg, ws_yaml = _load_or_die(ctx.obj["settings_path"], ctx.obj["workstations_path"])
    init_schema(Path(cfg.db_path))
    if ws_yaml:
        # Dev convenience — mirror yaml into DB so a fresh customer can
        # boot from a yaml-only seed. The Web UI mutates the DB directly
        # afterwards; subsequent boots can ignore yaml.
        do_sync(cfg.db_path, ws_yaml)

    if reload:
        # Reload mode requires an import string, not an app instance.
        import os
        os.environ["FLOW_HARVESTER_SETTINGS"] = ctx.obj["settings_path"]
        os.environ["FLOW_HARVESTER_WORKSTATIONS"] = ctx.obj["workstations_path"]
        os.environ["FLOW_HARVESTER_AUTO_START"] = "0" if no_auto_start else "1"
        os.environ["FLOW_HARVESTER_IDLE_POLL_SEC"] = str(idle_poll_sec)
        uvicorn.run(
            "app.web.bootstrap:app",
            host=host, port=port, reload=True, log_level="info",
        )
        return

    from app.web.server import create_app
    app = create_app(
        config=cfg,
        auto_start_daemon=not no_auto_start,
        idle_poll_sec=idle_poll_sec,
    )
    click.echo(f"flow-harvester serving at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


@cli.command("gen-license")
@click.option("--customer", required=True, help="客户标识，例如 acme-corp")
@click.option("--days", default=30, show_default=True, type=int,
              help="授权有效天数")
@click.option("--out", default="license.key", show_default=True,
              type=click.Path(dir_okay=False, writable=True),
              help="输出文件路径")
def gen_license(customer: str, days: int, out: str) -> None:
    """Dev-only: 给客户签发一个时限授权 license.key 文件。

    把生成出的 license.key 放进 ``dist\\FlowHarvester\\`` 一起打包发给
    客户，到期后再生成新的发过去即可（不用重新打包整个 bundle）。
    """
    import json

    from app.license import generate_license

    data = generate_license(customer, days)
    Path(out).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    click.echo(f"授权已生成: {out}")
    click.echo(f"  客户:     {data['customer_id']}")
    click.echo(f"  签发时间: {data['issued_at']}")
    click.echo(f"  过期时间: {data['expires_at']}")
    click.echo(f"  有效期:   {days} 天")


def main() -> None:  # pragma: no cover - thin entrypoint
    cli(obj={})


if __name__ == "__main__":  # pragma: no cover
    main()

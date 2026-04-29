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
        return load_config(settings, workstations)
    except ConfigError as exc:
        click.echo(f"[config error] {exc}", err=True)
        sys.exit(2)


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


def main() -> None:  # pragma: no cover - thin entrypoint
    cli(obj={})


if __name__ == "__main__":  # pragma: no cover
    main()

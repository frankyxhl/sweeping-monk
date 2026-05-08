"""Sweeping-Monk CLI — typer-based.

Today's commands (read-only on disk state):
    swm dashboard <repo>          render latest poll for each open PR
    swm history   <repo> [--pr N] timeline of status changes
    swm summary   <repo>          one-row-per-PR table
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.text import Text

from rich.table import Table

from . import dashboard, guarded
from .gh import GhClient, GhCommandError
from .poll import poll as run_poll
from .state import StateStore, default_store

app = typer.Typer(help="Sweeping-Monk PR watchdog CLI", no_args_is_help=True)
console = Console()


def _store(state_dir: Optional[str]) -> StateStore:
    return StateStore(Path(state_dir)) if state_dir else default_store()


@app.command("dashboard")
def dashboard_cmd(
    repo: str = typer.Argument(..., help="owner/repo, e.g. frankyxhl/trinity"),
    state_dir: Optional[str] = typer.Option(None, "--state-dir", help="Override state/ directory (tests)"),
) -> None:
    """Render latest poll for each PR as rich panels."""
    store = _store(state_dir)
    by_pr = store.latest_per_pr(repo)
    if not by_pr:
        console.print(f"[yellow]no recorded polls for {repo}[/yellow]")
        raise typer.Exit(code=1)
    for pr_num in sorted(by_pr):
        rec = by_pr[pr_num]
        snapshot = None
        if rec.threads:
            snapshot = store.read_thread(rec.repo, rec.pr, rec.threads[0].id)
        console.print(dashboard.pr_card(rec, snapshot))


@app.command()
def history(
    repo: str = typer.Argument(..., help="owner/repo"),
    pr: Optional[int] = typer.Option(None, "--pr", help="Filter to one PR number"),
    state_dir: Optional[str] = typer.Option(None, "--state-dir"),
) -> None:
    """Show chronological status transitions (collapses 'no change' runs)."""
    store = _store(state_dir)
    records = list(store.read_polls(repo, pr))
    if not records:
        scope = f" PR #{pr}" if pr else ""
        console.print(f"[yellow]no recorded polls for {repo}{scope}[/yellow]")
        raise typer.Exit(code=1)
    console.print(dashboard.history_table(records))


@app.command()
def summary(
    repo: str = typer.Argument(..., help="owner/repo"),
    state_dir: Optional[str] = typer.Option(None, "--state-dir"),
) -> None:
    """One row per open PR with status + Codex resolution counts."""
    store = _store(state_dir)
    by_pr = store.latest_per_pr(repo)
    if not by_pr:
        console.print(f"[yellow]no recorded polls for {repo}[/yellow]")
        raise typer.Exit(code=1)
    console.print(dashboard.summary_table(by_pr[k] for k in sorted(by_pr)))


@app.command("poll")
def poll_cmd(
    repo: str = typer.Argument(..., help="owner/repo, e.g. frankyxhl/trinity"),
    sync: bool = typer.Option(False, "--sync", help="Stage 1.5: resolve GitHub threads when local verdict=RESOLVED"),
    base: str = typer.Option("main", "--base", help="Base branch to filter PRs by"),
    state_dir: Optional[str] = typer.Option(None, "--state-dir"),
) -> None:
    """Run one full poll cycle: gh fetch → classify → judge → write JSONL → render."""
    store = _store(state_dir)
    gh_client = GhClient()
    outcomes = run_poll(repo, store=store, gh_client=gh_client, sync=sync, base=base)
    if not outcomes:
        console.print(f"[yellow]no open PRs in {repo} (base={base})[/yellow]")
        return
    for outcome in outcomes:
        console.print(dashboard.pr_card(outcome.record, outcome.snapshots[0] if outcome.snapshots else None))
    if any(o.sync_actions for o in outcomes):
        synced = sum(len(o.sync_actions) for o in outcomes)
        console.print(f"[green]Stage 1.5 sync: resolved {synced} thread(s) on GitHub[/green]")


def _confirm(prompt: str, *, yes: bool) -> bool:
    if yes:
        return True
    answer = typer.prompt(f"{prompt} [y/N]", default="n", show_default=False)
    return answer.strip().lower() in ("y", "yes")


def _abort(message: str) -> None:
    console.print(f"[red]✗ {message}[/red]")
    raise typer.Exit(code=1)


@app.command("approve")
def approve_cmd(
    repo: str = typer.Argument(..., help="owner/repo"),
    pr: int = typer.Argument(..., help="PR number"),
    reason: str = typer.Option(..., "--reason", help="One-line maintainer authorization phrase (lands in the ledger)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the [y/N] confirmation prompt"),
    state_dir: Optional[str] = typer.Option(None, "--state-dir"),
) -> None:
    """SWM-1103 — submit an APPROVE review on behalf of the maintainer.

    Refuses unless: identity is non-self, latest poll status=ready, head SHA fresh.
    """
    store = _store(state_dir)
    gh_client = GhClient()

    try:
        view = gh_client.view_pr(repo, pr, ["headRefOid", "author"])
    except GhCommandError as exc:
        _abort(str(exc))
    current_head = view.get("headRefOid", "")
    if not current_head:
        _abort(f"could not read current head SHA for {repo}#{pr}")

    try:
        identity = guarded.check_identity(gh_client, repo, pr)
    except GhCommandError as exc:
        _abort(str(exc))
    verdict = guarded.check_verdict(store, repo, pr, current_head)

    blockers: list[str] = []
    if identity.blocker:
        blockers.append(identity.blocker)
    ok, why = verdict.supports_approve()
    if not ok and why:
        blockers.append(why)
    if blockers:
        for b in blockers:
            console.print(f"[red]✗[/red] {b}")
        raise typer.Exit(code=1)

    poll = store.latest_poll(repo, pr)
    assert poll is not None  # supports_approve() already checked
    body = guarded.render_approve_body(poll, reason)
    console.print(f"[bold]Plan:[/bold] APPROVE {repo}#{pr} as [cyan]{identity.active_login}[/cyan] @ {current_head[:8]}")
    if not identity.is_preferred_identity:
        console.print(f"[yellow]⚠[/yellow] active account is not the preferred {guarded.PREFERRED_AGENT_LOGIN!r} — proceeding under explicit maintainer authorization")
    console.print(Text(body, style="dim"))
    if not _confirm("Submit?", yes=yes):
        console.print("[yellow]aborted[/yellow]")
        raise typer.Exit(code=1)

    # TOCTOU re-fetch (CHG-1104 §Risks row 1): the head SHA could have moved
    # while the maintainer was reading the prompt. Re-read immediately before
    # the review call and refuse if it drifted.
    try:
        recheck = gh_client.view_pr(repo, pr, ["headRefOid"])
    except GhCommandError as exc:
        _abort(str(exc))
    if recheck.get("headRefOid", "") != current_head:
        _abort(
            f"head SHA drifted during confirmation: {current_head[:8]} → "
            f"{(recheck.get('headRefOid') or '')[:8]} — re-poll first"
        )

    try:
        review_result = gh_client.submit_review_approve(repo, pr, body)
        verify = gh_client.view_pr(repo, pr, ["reviewDecision", "mergeStateStatus"])
    except GhCommandError as exc:
        _abort(str(exc))

    entry = guarded.build_approve_ledger_entry(
        poll=poll,
        actor=identity.active_login,
        reason=reason,
        authorized_by=f"maintainer (interactive --reason={reason!r})",
        review_result={"stdout": review_result.get("stdout", ""), **verify},
    )
    store.append_ledger(entry)
    console.print(
        f"[green]✓[/green] reviewDecision={verify.get('reviewDecision')} "
        f"mergeStateStatus={verify.get('mergeStateStatus')}"
    )
    console.print(f"[green]✓[/green] ledger appended to state/{repo}/pr-{pr}/ledger.jsonl")


@app.command("tick")
def tick_cmd(
    repo: str = typer.Argument(..., help="owner/repo"),
    pr: int = typer.Argument(..., help="PR number"),
    reason: str = typer.Option(..., "--reason", help="One-line maintainer authorization phrase (lands in the ledger)"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    state_dir: Optional[str] = typer.Option(None, "--state-dir"),
) -> None:
    """SWM-1103 — flip PR-body checkboxes whose claim is factually true per the latest poll.

    Each candidate box is matched against a small rule table; unmatched / unsatisfied
    boxes are left untouched.
    """
    store = _store(state_dir)
    gh_client = GhClient()

    try:
        view = gh_client.view_pr(repo, pr, ["headRefOid", "body", "author"])
    except GhCommandError as exc:
        _abort(str(exc))
    current_head = view.get("headRefOid", "")
    body = view.get("body") or ""
    if not current_head:
        _abort(f"could not read current head SHA for {repo}#{pr}")

    try:
        identity = guarded.check_identity(gh_client, repo, pr)
    except GhCommandError as exc:
        _abort(str(exc))
    verdict = guarded.check_verdict(store, repo, pr, current_head)
    blockers: list[str] = []
    if identity.blocker:
        blockers.append(identity.blocker)
    ok, why = verdict.supports_tick()
    if not ok and why:
        blockers.append(why)
    if blockers:
        for b in blockers:
            console.print(f"[red]✗[/red] {b}")
        raise typer.Exit(code=1)

    poll = store.latest_poll(repo, pr)
    assert poll is not None
    boxes = guarded.parse_unchecked_boxes(body)
    if not boxes:
        console.print("[yellow]no unchecked boxes in PR body[/yellow]")
        raise typer.Exit(code=0)

    classifications = [guarded.classify_box(b, poll) for b in boxes]
    table = Table(show_header=True, header_style="bold")
    table.add_column("L#", justify="right")
    table.add_column("Box")
    table.add_column("Rule")
    table.add_column("Action")
    table.add_column("Evidence", style="dim")
    for c in classifications:
        action = "[green]flip[/green]" if c.should_flip else (
            "[yellow]skip (not satisfied)[/yellow]" if c.rule_id else "[dim]skip (manual)[/dim]"
        )
        table.add_row(
            str(c.box.line_number),
            c.box.text[:60] + ("…" if len(c.box.text) > 60 else ""),
            c.rule_id or "—",
            action,
            c.reason,
        )
    console.print(table)

    flippable = [c for c in classifications if c.should_flip]
    if not flippable:
        console.print("[yellow]nothing to flip — all unchecked boxes are unverifiable or unsatisfied[/yellow]")
        raise typer.Exit(code=0)

    console.print(f"[bold]Plan:[/bold] flip {len(flippable)} box(es) on {repo}#{pr} @ {current_head[:8]}")
    if not _confirm("Apply?", yes=yes):
        console.print("[yellow]aborted[/yellow]")
        raise typer.Exit(code=1)

    new_body = guarded.apply_box_flips(body, [c.box.line_number for c in flippable])
    if new_body == body:
        _abort("apply_box_flips produced no diff — refusing to call gh pr edit")

    try:
        gh_client.edit_pr_body(repo, pr, new_body)
        verify = gh_client.view_pr(repo, pr, ["body"])
    except GhCommandError as exc:
        _abort(str(exc))
    if verify.get("body", "") != new_body:
        _abort("post-edit body does not match the prepared diff — ledger not written")

    entry = guarded.build_tick_ledger_entry(
        poll=poll,
        actor=identity.active_login,
        authorized_by=f"maintainer (interactive --reason={reason!r})",
        reason=reason,
        flipped=flippable,
    )
    store.append_ledger(entry)
    console.print(f"[green]✓[/green] flipped {len(flippable)} box(es); ledger appended")


@app.command("ledger")
def ledger_cmd(
    repo: str = typer.Argument(..., help="owner/repo"),
    pr: int = typer.Argument(..., help="PR number"),
    state_dir: Optional[str] = typer.Option(None, "--state-dir"),
) -> None:
    """Render the SWM-1103 audit trail of one-shot writes for a PR."""
    store = _store(state_dir)
    entries = store.read_ledger(repo, pr)
    if not entries:
        console.print(f"[yellow]no ledger entries for {repo}#{pr}[/yellow]")
        raise typer.Exit(code=0)
    table = Table(title=f"Ledger — {repo}#{pr}", show_header=True, header_style="bold")
    table.add_column("ts (UTC)")
    table.add_column("action")
    table.add_column("actor")
    table.add_column("head")
    table.add_column("reason", style="dim")
    for e in entries:
        ts = e.ts.strftime("%Y-%m-%d %H:%M:%S") if isinstance(e.ts, datetime) else str(e.ts)
        action = e.action.value if hasattr(e.action, "value") else str(e.action)
        table.add_row(ts, action, e.actor, e.head_sha[:8], e.reason)
    console.print(table)


if __name__ == "__main__":
    app()

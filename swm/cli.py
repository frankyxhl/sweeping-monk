"""Sweeping-Monk CLI — typer-based.

Today's commands (read-only on disk state):
    swm dashboard <repo>          render latest poll for each open PR
    swm history   <repo> [--pr N] timeline of status changes
    swm summary   <repo>          one-row-per-PR table
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from . import dashboard
from .gh import GhClient
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


if __name__ == "__main__":
    app()

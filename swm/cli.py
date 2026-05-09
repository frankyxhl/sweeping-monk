"""Sweeping-Monk CLI — typer-based.

Today's commands (read-only on disk state):
    swm dashboard <repo>          render latest poll for each open PR
    swm history   <repo> [--pr N] timeline of status changes
    swm summary   <repo>          one-row-per-PR table
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.text import Text

from rich.table import Table

from . import dashboard, guarded
from .close_reason import (
    build_thread_conclusion_comment,
    existing_conclusion_markers,
    has_flash_close_reason,
)
from .gh import GhClient, GhCommandError
from .github_app import InstallationTokenProvider
from .investigator import build_investigator_from_env
from .models import Thread, ThreadSnapshot, Verdict
from .poll import _maybe_sync, poll as run_poll, poll_pr
from .state import StateStore, default_store, now_utc
from .webhook import load_config, serve as serve_webhook

app = typer.Typer(help="Sweeping-Monk PR watchdog CLI", no_args_is_help=True)
webhook_app = typer.Typer(help="GitHub webhook receiver commands", no_args_is_help=True)
app.add_typer(webhook_app, name="webhook")
console = Console()


_REPO_RE = re.compile(r"^[^/]+/[^/]+$")


def _validate_repo(value: str | None) -> str | None:
    """Callback for typer.Argument: ensure repo looks like 'owner/name'.

    None passes through (some commands accept Optional[str] repo).
    Non-None must contain exactly one ``/`` separating non-empty owner and name.
    Raises typer.BadParameter on malformed input so the user sees a clean
    one-line error (exit 2) instead of a Python traceback from state.py.
    """
    if value is None:
        return value
    if not _REPO_RE.match(value):
        raise typer.BadParameter(
            f"expected 'owner/repo' format, got {value!r}",
            param_hint="repo",
        )
    return value


def _store(state_dir: Optional[str]) -> StateStore:
    return StateStore(Path(state_dir)) if state_dir else default_store()


def _gh_client(actor_name: str | None = None, *, config_path: str = "~/.config/swm/watchd.toml") -> GhClient:
    if not actor_name:
        return GhClient()
    config = load_config(config_path)
    actor = config.actor(actor_name)
    token = InstallationTokenProvider().token_for(
        app_id=actor.app_id,
        installation_id=actor.installation_id,
        private_key_path=actor.private_key_path,
        api_url=actor.api_url,
    )
    return GhClient(token=token, actor_login=actor.bot_login)


@app.command("dashboard")
def dashboard_cmd(
    repo: str = typer.Argument(..., help="owner/repo, e.g. frankyxhl/trinity", callback=_validate_repo),
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
    repo: str = typer.Argument(..., help="owner/repo", callback=_validate_repo),
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
    repo: str = typer.Argument(..., help="owner/repo", callback=_validate_repo),
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
    repo: str = typer.Argument(..., help="owner/repo, e.g. frankyxhl/trinity", callback=_validate_repo),
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
    # Codex P1 (3207007029): the `no change:` sentinel represents the WHOLE run.
    # If any PR in a multi-PR repo changed, the cron pattern `grep -q "^no change:"
    # && exit 0` would otherwise suppress the changed PR. Emit per-PR no-change
    # lines only when every outcome is unchanged.
    if all(o.is_no_change for o in outcomes):
        for outcome in outcomes:
            r = outcome.record
            console.print(
                f"no change: {r.repo}#{r.pr} still {r.status.value} "
                f"@ {r.head_sha[:8]} · codex_open={r.codex_open}"
            )
        return
    for outcome in outcomes:
        console.print(dashboard.pr_card(outcome.record, outcome.snapshots[0] if outcome.snapshots else None))
    if any(o.sync_actions for o in outcomes):
        synced = sum(len(o.sync_actions) for o in outcomes)
        console.print(f"[green]Stage 1.5 sync: resolved {synced} thread(s) on GitHub[/green]")


def _closable_threads(record_threads: list[Thread], snapshots: list[ThreadSnapshot]) -> list[Thread]:
    by_id = {snapshot.thread_id: snapshot for snapshot in snapshots}
    closable: list[Thread] = []
    for thread in record_threads:
        snapshot = by_id.get(thread.id)
        if not snapshot or not snapshot.github_state:
            continue
        if thread.verdict is Verdict.RESOLVED and not snapshot.github_state.isResolved:
            closable.append(thread)
    return closable


def _open_review_threads(record_threads: list[Thread], snapshots: list[ThreadSnapshot]) -> list[Thread]:
    by_id = {snapshot.thread_id: snapshot for snapshot in snapshots}
    review_threads: list[Thread] = []
    for thread in record_threads:
        snapshot = by_id.get(thread.id)
        if not snapshot or not snapshot.github_state:
            continue
        if not snapshot.github_state.isResolved:
            review_threads.append(thread)
    return review_threads


def _conclusion_reply_exists(
    comments: list[dict],
    thread: Thread,
    *,
    head_sha: str,
    actor_login: str | None,
) -> bool:
    markers = existing_conclusion_markers(thread, head_sha=head_sha)
    for comment in comments:
        body = str(comment.get("body") or "")
        if not any(marker in body for marker in markers):
            continue
        if not actor_login:
            return True
        user = comment.get("user") or comment.get("author") or {}
        if user.get("login") == actor_login:
            return True
    return False


def _thread_reaction_content(thread: Thread) -> str:
    return "+1" if thread.verdict is Verdict.RESOLVED else "-1"


def _clip_line(value: str | None, limit: int = 220) -> str:
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _build_close_items_run_comment(
    record,
    *,
    sync_count: int,
    actor: str | None,
    error: str | None = None,
) -> str:
    lines = [
        "<!-- swm:close-items-run -->",
        "Clearance run conclusion",
        "",
        f"- Status: `{record.status.value}`",
        f"- PR: `{record.repo}#{record.pr}`",
        f"- Head: `{record.head_sha[:12]}`",
        f"- Actor: `{actor or 'gh active account'}`",
        f"- Findings: `{record.codex_resolved}/{len(record.threads)}` resolved",
        f"- Closed this run: `{sync_count}`",
        f"- CI: `{', '.join(f'{name}={value.value}' for name, value in sorted(record.ci.items())) or 'none'}`",
        f"- Merge state: `{record.merge_state or 'unknown'}`",
    ]
    if error:
        lines.append(f"- Writeback error: `{_clip_line(error, 500)}`")
    if record.threads:
        lines.extend(["", "Review findings:"])
        for index, thread in enumerate(record.threads, start=1):
            location = f"{thread.path}:{thread.line}" if thread.line else thread.path
            lines.append(
                f"- #{index} `{thread.verdict.value}` {location} - "
                f"{_clip_line(thread.llm_reason or thread.verdict_reason)}"
            )
    else:
        lines.extend(["", "Review findings: none"])
    return "\n".join(lines)


@app.command("close-items")
def close_items_cmd(
    repo: str = typer.Argument(..., help="owner/repo, e.g. frankyxhl/alfred", callback=_validate_repo),
    pr: int = typer.Argument(..., help="PR number"),
    base: str = typer.Option("main", "--base", help="Base branch to filter PRs by"),
    actor: Optional[str] = typer.Option(None, "--actor", help="GitHub App actor from watchd config, e.g. clearance"),
    config: str = typer.Option("~/.config/swm/watchd.toml", "--config", help="watchd TOML config for --actor"),
    require_flash: bool = typer.Option(False, "--require-flash", help="Refuse to close any item without a Flash investigator reason"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the [y/N] confirmation prompt"),
    state_dir: Optional[str] = typer.Option(None, "--state-dir"),
) -> None:
    """Close GitHub review threads that SWM currently judges RESOLVED.

    This is a targeted Stage 1.5 writeback for one PR. It does not approve,
    comment, tick checkboxes, or merge.
    """
    store = _store(state_dir)
    gh_client = _gh_client(actor, config_path=config)

    try:
        open_prs = gh_client.list_open_prs(repo, base=base)
        pr_summary = next(
            (
                item for item in open_prs
                if item.get("number") == pr
                and not item.get("isDraft")
                and item.get("baseRefName") == base
            ),
            None,
        )
        if pr_summary is None:
            _abort(f"no open non-draft PR #{pr} in {repo} (base={base})")

        branch_protection = gh_client.branch_protection(repo, base)
        record, snapshots = poll_pr(
            pr_summary,
            repo=repo,
            gh_client=gh_client,
            branch_protected=branch_protection is not None,
            investigator=build_investigator_from_env(),
            now=now_utc(),
        )
    except GhCommandError as exc:
        _abort(str(exc))

    open_threads = _open_review_threads(record.threads, snapshots)
    closable = _closable_threads(record.threads, snapshots)
    snapshots_by_id = {snapshot.thread_id: snapshot for snapshot in snapshots}
    if not open_threads:
        record = record.model_copy(update={"trigger": "manual-close-items"})
        store.append_poll(record)
        for snapshot in snapshots:
            store.write_thread(snapshot)
        gh_client.create_issue_comment(
            repo,
            pr,
            _build_close_items_run_comment(record, sync_count=0, actor=actor),
        )
        console.print(f"[yellow]no open review items for {repo}#{pr}[/yellow]")
        console.print(dashboard.pr_card(record, snapshots[0] if snapshots else None))
        return

    table = Table(title=f"Open review items — {repo}#{pr}", show_header=True, header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Thread")
    table.add_column("Location")
    table.add_column("Verdict")
    table.add_column("Verifier")
    table.add_column("Reason", style="dim")
    for index, thread in enumerate(open_threads, start=1):
        snapshot = snapshots_by_id.get(thread.id)
        line = f":{thread.line}" if thread.line else ""
        table.add_row(
            str(index),
            thread.id,
            f"{thread.path}{line}",
            thread.verdict.value,
            "Flash" if has_flash_close_reason(thread, snapshot) else "SWM",
            thread.verdict_reason,
        )
    console.print(table)
    console.print(
        f"[bold]Plan:[/bold] mark {len(open_threads)} review item(s), "
        f"close {len(closable)} resolved item(s), on "
        f"{repo}#{pr} @ {record.head_sha[:8]}"
    )
    if not _confirm("Apply?", yes=yes):
        console.print("[yellow]aborted[/yellow]")
        raise typer.Exit(code=1)

    current_pr = gh_client.view_pr(repo, pr, ["headRefOid"])
    current_head = current_pr.get("headRefOid", "")
    if current_head != record.head_sha:
        _abort(
            f"PR head changed since poll ({record.head_sha[:8]} → {current_head[:8]}); "
            "re-run close-items to poll the new head"
        )

    for thread in open_threads:
        snapshot = snapshots_by_id.get(thread.id)
        if require_flash and thread.verdict is Verdict.RESOLVED and not has_flash_close_reason(thread, snapshot):
            _abort(f"{thread.id} has no Flash investigator reason; refusing to close")
        if thread.comment_id <= 0:
            _abort(f"{thread.id} has no GitHub review comment id; refusing to write a conclusion")

    reply_results: dict[str, dict] = {}
    reaction_results: dict[str, dict] = {}
    actions = []
    try:
        existing_comments = gh_client.pulls_comments(repo, pr)
        actor_login = gh_client.actor_login
        for thread in open_threads:
            snapshot = snapshots_by_id.get(thread.id)
            reaction_results[thread.id] = gh_client.set_review_comment_reaction(
                repo,
                thread.comment_id,
                _thread_reaction_content(thread),
            )
            if _conclusion_reply_exists(
                existing_comments,
                thread,
                head_sha=record.head_sha,
                actor_login=actor_login,
            ):
                reply_results[thread.id] = {"skipped": True, "reason": "existing Clearance conclusion for this head"}
            else:
                body = build_thread_conclusion_comment(thread, snapshot, head_sha=record.head_sha)
                reply_results[thread.id] = gh_client.reply_to_review_comment(
                    repo, pr, thread.comment_id, body,
                )
        actions = _maybe_sync(record, snapshots, gh_client=gh_client)
    except GhCommandError as exc:
        record = record.model_copy(update={
            "stage15_actions": actions,
            "trigger": "manual-close-items+writeback-error",
        })
        store.append_poll(record)
        for snapshot in snapshots:
            store.write_thread(snapshot)
        try:
            gh_client.create_issue_comment(
                repo,
                pr,
                _build_close_items_run_comment(
                    record,
                    sync_count=len(actions),
                    actor=actor,
                    error=str(exc),
                ),
            )
        except GhCommandError as comment_exc:
            console.print(f"[red]✗ failed to append run conclusion: {comment_exc}[/red]")
        _abort(str(exc))

    if reply_results:
        actions = [
            action.model_copy(update={
                "result": {
                    **action.result,
                    "close_reason_comment": reply_results.get(action.threadId, {}),
                    "reaction": reaction_results.get(action.threadId, {}),
                },
            })
            for action in actions
        ]
    record = record.model_copy(update={
        "stage15_actions": actions,
        "trigger": "manual-close-items+stage1.5-sync" if actions else "manual-close-items",
    })
    store.append_poll(record)
    for snapshot in snapshots:
        store.write_thread(snapshot)
    gh_client.create_issue_comment(
        repo,
        pr,
        _build_close_items_run_comment(record, sync_count=len(actions), actor=actor),
    )

    console.print(f"[green]✓[/green] closed {len(actions)} review item(s) on GitHub")
    console.print(dashboard.pr_card(record, snapshots[0] if snapshots else None))


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
    repo: str = typer.Argument(..., help="owner/repo", callback=_validate_repo),
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
    repo: str = typer.Argument(..., help="owner/repo", callback=_validate_repo),
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

    # CHG-1105: persist every skipped classification as a box-miss observation.
    # Misses are observations of what the classifier saw — recorded regardless
    # of whether the user later confirms the flip. `poll.head_sha == current_head`
    # here by `supports_tick()`'s freshness gate; `build_box_miss` uses the
    # poll's head, which is the SHA the classifier actually classified against.
    for c in classifications:
        if not c.should_flip:
            store.append_box_miss(guarded.build_box_miss(classification=c, poll=poll))

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
    repo: str = typer.Argument(..., help="owner/repo", callback=_validate_repo),
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


_SINCE_RE = re.compile(r"^(\d+)([dhm])$")


def _parse_since(since: str) -> timedelta:
    """`Nd` / `Nh` / `Nm` → timedelta. Raises typer.BadParameter on bad input
    so the user sees a clean one-line error instead of a Python traceback."""
    m = _SINCE_RE.match(since.strip())
    if not m:
        raise typer.BadParameter(f"expected Nd / Nh / Nm, got {since!r}", param_hint="--since")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(days=n) if unit == "d" else timedelta(hours=n) if unit == "h" else timedelta(minutes=n)


def _canonicalize_box_text(text: str) -> str:
    """CHG-1105: lowercase + collapse internal whitespace. No punctuation strip."""
    return " ".join(text.lower().split())


@app.command("rule-coverage")
def rule_coverage_cmd(
    repo: Optional[str] = typer.Argument(None, help="owner/repo (optional; default: every repo)", callback=_validate_repo),
    since: str = typer.Option("7d", "--since", help="Time window: Nd / Nh / Nm"),
    threshold: int = typer.Option(3, "--threshold", help="Hide rows with count < N"),
    state_dir: Optional[str] = typer.Option(None, "--state-dir"),
) -> None:
    """CHG-1105 — surface classifier blind spots from accumulated `box-misses.jsonl`.

    Groups misses by canonical box text (lowercased + whitespace-collapsed),
    sorts by count descending, hides rows below `--threshold` (default 3) and
    older than `--since` (default 7 days). For each group, reports the rule_id
    seen on the latest miss — `—` means no BOX_RULES regex matched (coverage
    gap), a rule_id means the regex matched but the predicate refused.
    """
    store = _store(state_dir)
    cutoff = now_utc() - _parse_since(since)
    misses = [m for m in store.read_box_misses(repo) if m.ts >= cutoff]
    if not misses:
        scope = f" {repo}" if repo else ""
        console.print(f"[yellow]no box misses recorded for{scope} (since {since})[/yellow]")
        raise typer.Exit(code=0)

    groups: dict[str, list] = {}
    for m in misses:
        groups.setdefault(_canonicalize_box_text(m.box_text), []).append(m)

    rows = [(canon, ms) for canon, ms in groups.items() if len(ms) >= threshold]
    rows.sort(key=lambda r: -len(r[1]))
    if not rows:
        console.print(
            f"[yellow]no patterns hit threshold {threshold} (try --threshold 1)[/yellow]"
        )
        raise typer.Exit(code=0)

    table = Table(
        title=f"Box-miss coverage — since {since}, threshold {threshold}",
        show_header=True, header_style="bold",
    )
    table.add_column("count", justify="right")
    table.add_column("canonical text")
    table.add_column("matched_rule")
    table.add_column("last seen (UTC)", style="dim")
    for canon, ms in rows:
        latest = max(ms, key=lambda m: m.ts)
        rule = latest.rule_id or "—"
        table.add_row(str(len(ms)), canon[:80], rule, latest.ts.strftime("%Y-%m-%d %H:%M"))
    console.print(table)


@webhook_app.command("serve")
def webhook_serve_cmd(
    config: str = typer.Option(
        "~/.config/swm/watchd.toml",
        "--config",
        help="watchd TOML config with server, actors, and watched repos",
    ),
) -> None:
    """Serve GitHub App webhook events for Clearance-style automation."""
    cfg = load_config(config)
    console.print(
        f"[green]serving[/green] {cfg.server.path} on "
        f"{cfg.server.host}:{cfg.server.port}"
    )
    serve_webhook(cfg)


if __name__ == "__main__":
    app()

"""Poll orchestrator — the deterministic core of SWM-1100.

Flow per repo:
  1. gh.list_open_prs(repo)
  2. for each PR: collect inputs (view + inline comments + GraphQL threads)
  3. classify each Codex thread (A/B/C) → severity → verdict
  4. compute PR status from (CI, local findings, thread verdicts)
  5. write PollRecord to StateStore (and ThreadSnapshot per Codex thread)
  6. if sync=True and Stage 1.5 active: call gh.resolve_thread for newly-RESOLVED
     threads whose GitHub state is still isResolved=false.

The 'AI judgment' part of SWM-1101 (substantive-reasonableness) is delegated
to swm.judge today via a simple regex heuristic; the integration point is
isolated so it can be swapped for a Claude-API call without touching this
orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import classify, judge, severity
from .gh import GhClient
from .models import (
    CIConclusion,
    Evidence,
    GitHubThreadState,
    PollRecord,
    Stage15Action,
    Status,
    Thread,
    ThreadSnapshot,
    Verdict,
    VerdictHistoryEntry,
)
from .state import StateStore, now_utc

# An empty statusCheckRollup is ambiguous: either GitHub Actions decided not to
# trigger any workflow (paths-ignore matched the whole diff — intentional skip),
# or the runner hasn't picked up the push yet. Within this grace period after
# the PR's updatedAt, treat empty CI as 'in_progress' to avoid prematurely
# flipping a docs-only PR to ready while runners are warming. Past the window,
# treat empty as 'absent' (paths-ignore working as designed).
CI_EMPTY_GRACE = timedelta(minutes=5)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    # Python 3.11+ handles trailing 'Z' via fromisoformat.
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class PollOutcome:
    record: PollRecord
    snapshots: list[ThreadSnapshot]
    sync_actions: list[Stage15Action]
    is_no_change: bool = False


def _ci_dict(rollup: list[dict]) -> dict[str, CIConclusion]:
    out: dict[str, CIConclusion] = {}
    for entry in rollup or []:
        name = entry.get("name") or entry.get("workflowName") or "unknown"
        conclusion = entry.get("conclusion") or entry.get("status") or "PENDING"
        try:
            out[name] = CIConclusion(conclusion)
        except ValueError:
            out[name] = CIConclusion.PENDING
    return out


def _ci_status_class(
    ci: dict[str, CIConclusion],
    *,
    pr_updated_at: datetime | None = None,
    now: datetime | None = None,
    grace: timedelta = CI_EMPTY_GRACE,
) -> str:
    """Returns one of: 'green', 'failure', 'in_progress', 'absent'.

    Empty `ci` is split by the grace window: within `grace` after pr_updated_at
    we assume the runner is still picking up the push ('in_progress'); past it
    we assume the workflow was deliberately skipped ('absent').
    """
    if not ci:
        if pr_updated_at is None or now is None:
            return "absent"
        if now - pr_updated_at < grace:
            return "in_progress"
        return "absent"
    values = list(ci.values())
    if any(v is CIConclusion.FAILURE or v is CIConclusion.CANCELLED for v in values):
        return "failure"
    if any(v is CIConclusion.IN_PROGRESS or v is CIConclusion.PENDING for v in values):
        return "in_progress"
    if all(v is CIConclusion.SUCCESS or v is CIConclusion.SKIPPED or v is CIConclusion.NEUTRAL for v in values):
        return "green"
    return "in_progress"


def _classify_finding_kind(thread: dict) -> str | None:
    """Map a Codex thread's body to a SWM-1102 finding_kind. Conservative: only
    return a known kind when the body contains a strong cue. Returns None for
    everything we haven't seen yet (severity passes through unchanged).
    """
    comments = (thread.get("comments") or {}).get("nodes") or []
    if not comments:
        return None
    body = (comments[0].get("body") or "").lower()
    if "required" in body and ("check" in body or "status" in body) and "paths-ignore" in body:
        return "required_check_coupling"
    return None


def _codex_severity_from_body(thread: dict) -> str:
    """Codex P-badges live in the comment body as `P1`/`P2`/`P3`. Default P3."""
    comments = (thread.get("comments") or {}).get("nodes") or []
    if not comments:
        return "P3"
    body = comments[0].get("body") or ""
    for sev in ("P1", "P2", "P3"):
        if f"![{sev} Badge]" in body or f"|{sev}|" in body or f"**{sev}**" in body:
            return sev
    return "P3"


def _process_thread(
    thread: dict,
    *,
    repo: str,
    pr: int,
    branch_protected: bool,
    now,
) -> tuple[Thread, ThreadSnapshot]:
    state = classify.classify_thread(thread)
    finding_kind = _classify_finding_kind(thread)
    codex_sev_str = _codex_severity_from_body(thread)
    from .models import Severity
    codex_sev = Severity(codex_sev_str)

    sev_decision = severity.evaluate(
        codex_severity=codex_sev,
        finding_kind=finding_kind,
        branch_protected=branch_protected,
    )

    reply = classify.latest_author_reply(thread)
    followup = classify.latest_codex_followup(thread)

    verdict_decision = judge.judge(
        classification=state,
        author_reply_body=(reply or {}).get("body"),
        code_changed=(state == "B"),
        codex_followup_body=(followup or {}).get("body"),
        github_isResolved=bool(thread.get("isResolved")),
    )

    comment_id = classify.codex_comment_id(thread) or 0
    path = thread.get("path") or "unknown"
    line = thread.get("line")

    thread_model = Thread(
        id=thread["id"],
        comment_id=comment_id,
        path=path,
        line=line,
        codex_severity=sev_decision.codex_severity,
        effective_severity=sev_decision.effective_severity,
        verdict=verdict_decision.verdict,
        verdict_reason=verdict_decision.reason,
        github_isResolved=bool(thread.get("isResolved")),
        author_reply_id=(reply or {}).get("databaseId"),
        author_reply_substantive=verdict_decision.substantive,
        code_changed=(state == "B"),
        demotion_reason=sev_decision.reason,
    )

    snapshot = ThreadSnapshot(
        thread_id=thread["id"],
        repo=repo,
        pr=pr,
        first_seen=now,
        last_polled=now,
        codex_comment_id=comment_id,
        path=path,
        current_line=line,
        codex_severity=sev_decision.codex_severity,
        effective_severity=sev_decision.effective_severity,
        demotion_reason=sev_decision.reason,
        verdict=verdict_decision.verdict,
        verdict_history=[VerdictHistoryEntry(
            ts=now, verdict=verdict_decision.verdict, reason=verdict_decision.reason,
        )],
        evidence=Evidence(
            thread_state=state,
            author_reply_id=(reply or {}).get("databaseId"),
            author_reply_substantive=verdict_decision.substantive,
            code_changed=(state == "B"),
            codex_followed_up=bool(followup),
            demotion_reason=sev_decision.reason,
        ),
        github_state=GitHubThreadState(
            isResolved=bool(thread.get("isResolved")),
            isOutdated=bool(thread.get("isOutdated")),
        ),
    )
    return thread_model, snapshot


def _compute_status(
    ci: dict[str, CIConclusion],
    threads: list[Thread],
    *,
    pr_updated_at: datetime | None = None,
    now: datetime | None = None,
    codex_signal: str | None = None,
) -> Status:
    ci_class = _ci_status_class(ci, pr_updated_at=pr_updated_at, now=now)
    if ci_class == "failure":
        return Status.BLOCKED

    # P1/P2 effective-severity OPEN findings → blocked.
    for t in threads:
        if t.verdict is Verdict.OPEN and t.effective_severity.value in ("P1", "P2"):
            return Status.BLOCKED

    # Codex 'reviewing' (👀) overrides everything else short of a hard failure:
    # the bot itself says it hasn't finished, so don't flip ready prematurely.
    if codex_signal == "reviewing":
        return Status.PENDING

    open_threads = [t for t in threads if t.verdict is not Verdict.RESOLVED]
    if open_threads:
        return Status.PENDING

    # Codex 'approved' (👍) lets a fresh PR with paths-ignore-skipped CI go
    # ready early — the bot has reviewed and signed off, so we don't need to
    # wait out the CI grace window. But a 👍 cannot override real CI that is
    # actually running (a check might still fail), so only apply this when
    # the rollup is empty (paths-ignore case).
    if codex_signal == "approved" and not ci:
        return Status.READY

    if ci_class == "in_progress":
        return Status.PENDING

    # ci_class is 'green' or 'absent' (paths-ignore intentional skip)
    return Status.READY


def poll_pr(
    pr_summary: dict,
    *,
    repo: str,
    gh_client: GhClient,
    branch_protected: bool,
    now,
) -> tuple[PollRecord, list[ThreadSnapshot]]:
    """Build a PollRecord + ThreadSnapshots for a single open PR."""
    pr = pr_summary["number"]
    head_sha = pr_summary["headRefOid"]
    raw_threads = gh_client.review_threads(repo, pr)
    codex_threads = [t for t in raw_threads if classify.is_codex_thread(t)]
    thread_models: list[Thread] = []
    snapshots: list[ThreadSnapshot] = []
    for t in codex_threads:
        m, snap = _process_thread(t, repo=repo, pr=pr, branch_protected=branch_protected, now=now)
        thread_models.append(m)
        snapshots.append(snap)

    body_reactions = gh_client.pr_body_reactions(repo, pr)
    codex_signal = classify.codex_pr_body_signal(body_reactions)

    ci = _ci_dict(pr_summary.get("statusCheckRollup") or [])
    pr_updated_at = _parse_iso(pr_summary.get("updatedAt"))
    status = _compute_status(
        ci, thread_models,
        pr_updated_at=pr_updated_at, now=now, codex_signal=codex_signal,
    )
    open_n = sum(1 for t in thread_models if t.verdict is not Verdict.RESOLVED)
    resolved_n = sum(1 for t in thread_models if t.verdict is Verdict.RESOLVED)

    record = PollRecord(
        ts=now,
        repo=repo,
        pr=pr,
        title=pr_summary.get("title"),
        head_sha=head_sha,
        status=status,
        ci=ci,
        merge_state=pr_summary.get("mergeStateStatus"),
        codex_open=open_n,
        codex_resolved=resolved_n,
        codex_pr_body_signal=codex_signal,
        threads=thread_models,
        trigger="poll-cycle",
    )
    return record, snapshots


def _maybe_sync(
    record: PollRecord,
    snapshots: list[ThreadSnapshot],
    *,
    gh_client: GhClient,
) -> list[Stage15Action]:
    """Stage 1.5 sync — call resolveReviewThread for any RESOLVED thread whose
    GitHub state is still isResolved=false. Per CLAUDE.md Stage 1.5, callers
    must verify thread state immediately before mutating; we do that via the
    snapshot's github_state.isResolved which was just read by review_threads().
    """
    actions: list[Stage15Action] = []
    by_id = {s.thread_id: s for s in snapshots}
    for thread in record.threads:
        snap = by_id.get(thread.id)
        if not snap or not snap.github_state:
            continue
        if thread.verdict is Verdict.RESOLVED and not snap.github_state.isResolved:
            result = gh_client.resolve_thread(thread.id)
            actions.append(Stage15Action(
                mutation="resolveReviewThread", threadId=thread.id, result=result or {},
            ))
            snap.github_state = GitHubThreadState(
                isResolved=True,
                isOutdated=snap.github_state.isOutdated,
                resolvedBy=(result or {}).get("resolvedBy", {}).get("login") if result else None,
                synced_via="Stage 1.5 resolveReviewThread",
                synced_at=record.ts,
            )
            thread.github_isResolved = True
    return actions


def poll(
    repo: str,
    *,
    store: StateStore,
    gh_client: GhClient,
    sync: bool = False,
    base: str = "main",
) -> list[PollOutcome]:
    """Run one full poll cycle for `repo` and persist results to `store`."""
    now = now_utc()
    open_prs = gh_client.list_open_prs(repo)
    open_prs = [pr for pr in open_prs if pr.get("baseRefName") == base and not pr.get("isDraft")]
    branch_protection = gh_client.branch_protection(repo, base)
    branch_protected = branch_protection is not None

    outcomes: list[PollOutcome] = []
    for pr_summary in open_prs:
        record, snapshots = poll_pr(
            pr_summary,
            repo=repo,
            gh_client=gh_client,
            branch_protected=branch_protected,
            now=now,
        )
        actions: list[Stage15Action] = []
        if sync:
            actions = _maybe_sync(record, snapshots, gh_client=gh_client)
            if actions:
                record = record.model_copy(update={
                    "stage15_actions": actions,
                    "trigger": "poll-cycle+stage1.5-sync",
                })

        # CHG-1107: short-circuit — compare state_key with prior poll
        prior = store.latest_poll(repo, record.pr)
        no_change = prior is not None and prior.state_key() == record.state_key()

        store.append_poll(record)
        for snap in snapshots:
            store.write_thread(snap)
        outcomes.append(PollOutcome(
            record=record, snapshots=snapshots, sync_actions=actions,
            is_no_change=no_change,
        ))
    return outcomes

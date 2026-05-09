"""Integration tests for the poll orchestrator — uses FakeGhClient (conftest)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swm.investigator import InvestigationDecision
from swm.models import Status, Verdict
from swm.poll import poll, poll_pr
from swm.state import StateStore
from tests.conftest import FakeGhClient


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr_summary(*, number: int = 49, head: str = "abc123", base: str = "main",
                draft: bool = False, ci: list[dict] | None = None,
                title: str = "ci: skip docs") -> dict:
    return {
        "number": number,
        "title": title,
        "headRefOid": head + "0" * (40 - len(head)),
        "baseRefName": base,
        "isDraft": draft,
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": ci or [],
        "updatedAt": "2026-05-07T12:00:00Z",
    }


def _codex_thread(*, thread_id: str = "T1", isOutdated: bool = False, isResolved: bool = False,
                  body: str = "P2 finding", reply_body: str | None = None) -> dict:
    comments = [{
        "databaseId": 1001,
        "author": {"login": "chatgpt-codex-connector"},
        "body": body,
        "createdAt": "2026-05-07T12:00:00Z",
        "replyTo": None,
    }]
    if reply_body:
        comments.append({
            "databaseId": 2002,
            "author": {"login": "ryosaeba1985"},
            "body": reply_body,
            "createdAt": "2026-05-07T12:30:00Z",
            "replyTo": {"databaseId": 1001},
        })
    return {
        "id": thread_id,
        "isResolved": isResolved,
        "isOutdated": isOutdated,
        "path": ".github/workflows/test.yml",
        "line": 31,
        "comments": {"nodes": comments},
    }


class FakeInvestigator:
    def __init__(self, decision: InvestigationDecision):
        self.decision = decision
        self.inputs = []

    def investigate(self, item):
        self.inputs.append(item)
        return self.decision


def test_poll_writes_one_record_per_open_pr(store: StateStore) -> None:
    # Arrange — two open PRs, no Codex threads
    gh = FakeGhClient(prs=[_pr_summary(number=49), _pr_summary(number=50, head="def456")])

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert
    assert len(outcomes) == 2
    persisted = list(store.read_polls())
    assert {r.pr for r in persisted} == {49, 50}


def test_poll_skips_drafts(store: StateStore) -> None:
    # Arrange
    gh = FakeGhClient(prs=[
        _pr_summary(number=49),
        _pr_summary(number=50, draft=True),
    ])

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert
    assert {o.record.pr for o in outcomes} == {49}


def test_poll_skips_prs_not_targeting_base(store: StateStore) -> None:
    # Arrange — one PR targets dev/, only main should land
    gh = FakeGhClient(prs=[
        _pr_summary(number=49, base="main"),
        _pr_summary(number=50, base="dev"),
    ])

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert
    assert {o.record.pr for o in outcomes} == {49}


def test_poll_marks_pending_when_p3_codex_thread_open(store: StateStore) -> None:
    # Arrange — P3 finding (non-blocking) with no author response yet → state A
    gh = FakeGhClient(
        prs=[_pr_summary(ci=[{"name": "ci", "conclusion": "SUCCESS"}])],
        review_threads={49: [_codex_thread(body="**P3** style nit")]},
    )

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert — P3 OPEN doesn't block, but it does prevent ready
    record = outcomes[0].record
    assert record.status is Status.PENDING
    assert record.threads[0].verdict is Verdict.OPEN
    assert record.threads[0].effective_severity.value == "P3"


def test_poll_marks_blocked_when_p2_finding_unresolved_with_protection(store: StateStore) -> None:
    # Arrange — branch protected → P2 stays P2 → blocked
    gh = FakeGhClient(
        prs=[_pr_summary(ci=[{"name": "ci", "conclusion": "SUCCESS"}])],
        review_threads={49: [_codex_thread(body="**P2** required check satisfiability paths-ignore concern")]},
        branch_protection_data={"required_status_checks": {"contexts": ["ci"]}},
    )

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert
    record = outcomes[0].record
    assert record.status is Status.BLOCKED
    assert record.threads[0].effective_severity.value == "P2"


def test_poll_demotes_p2_to_p3_when_no_branch_protection(store: StateStore) -> None:
    # Arrange — no branch protection → P2 demotes to P3 → pending (not blocked)
    gh = FakeGhClient(
        prs=[_pr_summary(ci=[{"name": "ci", "conclusion": "SUCCESS"}])],
        review_threads={49: [_codex_thread(body="**P2** required check satisfiability paths-ignore concern")]},
        branch_protection_data=None,
    )

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert
    record = outcomes[0].record
    assert record.status is Status.PENDING
    assert record.threads[0].codex_severity.value == "P2"
    assert record.threads[0].effective_severity.value == "P3"


def test_poll_marks_ready_when_all_threads_resolved_and_ci_green(store: StateStore) -> None:
    # Arrange — outdated thread (state B → RESOLVED) + green CI
    gh = FakeGhClient(
        prs=[_pr_summary(ci=[{"name": "linux", "conclusion": "SUCCESS"}, {"name": "macos", "conclusion": "SUCCESS"}])],
        review_threads={49: [_codex_thread(isOutdated=True, body="**P3** style nit")]},
    )

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert
    record = outcomes[0].record
    assert record.status is Status.READY
    assert record.threads[0].verdict is Verdict.RESOLVED


def test_poll_investigator_can_resolve_replied_thread(store: StateStore) -> None:
    investigator = FakeInvestigator(
        InvestigationDecision(
            verdict="RESOLVED",
            confidence=0.92,
            reason="diff removes the reviewed token logging",
            evidence=["print(token) was removed"],
        )
    )
    gh = FakeGhClient(
        prs=[_pr_summary(ci=[{"name": "ci", "conclusion": "SUCCESS"}])],
        review_threads={49: [_codex_thread(body="**P3** token logging concern", reply_body="Fixed it.")]},
        pr_diffs={49: "diff --git a/app.py b/app.py\n- print(token)\n+ logger.info('ok')\n"},
    )

    outcomes = poll("owner/repo", store=store, gh_client=gh, investigator=investigator)

    record = outcomes[0].record
    assert record.status is Status.READY
    assert record.threads[0].verdict is Verdict.RESOLVED
    assert record.threads[0].llm_verdict == "RESOLVED"
    assert record.threads[0].llm_confidence == 0.92
    assert investigator.inputs[0].diff_excerpt.startswith("diff --git")


def test_poll_investigator_can_keep_outdated_thread_open(store: StateStore) -> None:
    investigator = FakeInvestigator(
        InvestigationDecision(
            verdict="OPEN",
            confidence=0.88,
            reason="diff does not address the reviewed workflow concern",
            evidence=["only README changed"],
        )
    )
    gh = FakeGhClient(
        prs=[_pr_summary(ci=[{"name": "ci", "conclusion": "SUCCESS"}])],
        review_threads={49: [_codex_thread(isOutdated=True, body="**P3** workflow concern")]},
        pr_diffs={49: "diff --git a/README.md b/README.md\n+ docs only\n"},
    )

    outcomes = poll("owner/repo", store=store, gh_client=gh, investigator=investigator)

    record = outcomes[0].record
    assert record.status is Status.PENDING
    assert record.threads[0].verdict is Verdict.OPEN
    assert record.threads[0].verdict_reason == "LLM investigator: diff does not address the reviewed workflow concern"


def test_poll_marks_ready_when_no_codex_threads_and_old_pr_with_no_ci(store: StateStore) -> None:
    """Docs-only PR aged past the grace window — paths-ignore correctly inferred."""
    # Arrange — updatedAt 1 hour ago (well past 5-min grace)
    old_pr = _pr_summary(ci=[])
    old_pr["updatedAt"] = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    gh = FakeGhClient(prs=[old_pr])

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert — past grace + empty CI = absent → ready
    assert outcomes[0].record.status is Status.READY


def test_poll_marks_pending_when_pr_just_updated_and_ci_still_empty(store: StateStore) -> None:
    """A freshly-pushed PR with empty CI is still in the 5-min grace window."""
    # Arrange — updatedAt 1 minute ago (within grace)
    fresh_pr = _pr_summary(ci=[])
    fresh_pr["updatedAt"] = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    gh = FakeGhClient(prs=[fresh_pr])

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert — empty CI within grace = in_progress → pending
    assert outcomes[0].record.status is Status.PENDING


def test_poll_marks_pending_when_codex_eyes_reacts(store: StateStore) -> None:
    """Codex 👀 = 'still reviewing' — don't flip ready even if CI is past grace."""
    # Arrange — old PR (would be ready by other rules) but Codex is still reviewing
    pr = _pr_summary(ci=[{"name": "ci", "conclusion": "SUCCESS"}])
    pr["updatedAt"] = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    gh = FakeGhClient(
        prs=[pr],
        pr_body_reactions={49: [{
            "content": "EYES",
            "user": {"login": "chatgpt-codex-connector[bot]"},
            "createdAt": "2026-05-07T12:00:00Z",
        }]},
    )

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert
    record = outcomes[0].record
    assert record.status is Status.PENDING
    assert record.codex_pr_body_signal == "reviewing"


def test_poll_marks_ready_when_codex_thumbs_up_reacts_within_grace(store: StateStore) -> None:
    """Codex 👍 is a stronger signal than CI grace — fresh PR can go ready early."""
    # Arrange — fresh PR (within grace) but Codex already approved
    pr = _pr_summary(ci=[])
    pr["updatedAt"] = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    gh = FakeGhClient(
        prs=[pr],
        pr_body_reactions={49: [{
            "content": "THUMBS_UP",
            "user": {"login": "chatgpt-codex-connector[bot]"},
            "createdAt": "2026-05-07T12:00:00Z",
        }]},
    )

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert — Codex 👍 overrides "still in CI grace window"
    record = outcomes[0].record
    assert record.status is Status.READY
    assert record.codex_pr_body_signal == "approved"


def test_poll_thumbs_up_does_not_override_open_threads(store: StateStore) -> None:
    """Codex 👍 on body doesn't auto-resolve unresolved inline threads."""
    # Arrange — body 👍 but a P3 thread is still OPEN
    pr = _pr_summary(ci=[{"name": "ci", "conclusion": "SUCCESS"}])
    gh = FakeGhClient(
        prs=[pr],
        review_threads={49: [_codex_thread(body="**P3** style nit")]},
        pr_body_reactions={49: [{
            "content": "THUMBS_UP",
            "user": {"login": "chatgpt-codex-connector[bot]"},
            "createdAt": "2026-05-07T12:00:00Z",
        }]},
    )

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert — open threads still gate the verdict
    assert outcomes[0].record.status is Status.PENDING


def test_poll_treats_unparseable_updated_at_as_old(store: StateStore) -> None:
    """Garbage updatedAt should not crash and should default to 'past grace'."""
    # Arrange
    pr = _pr_summary(ci=[])
    pr["updatedAt"] = "not-a-timestamp"
    gh = FakeGhClient(prs=[pr])

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert — couldn't parse → no grace logic → absent → ready
    assert outcomes[0].record.status is Status.READY


def test_poll_sync_calls_resolve_thread_when_local_resolved_but_github_open(store: StateStore) -> None:
    # Arrange — outdated thread, github_isResolved=False, local verdict will be RESOLVED
    gh = FakeGhClient(
        prs=[_pr_summary(ci=[{"name": "linux", "conclusion": "SUCCESS"}])],
        review_threads={49: [_codex_thread(thread_id="PRRT_T1", isOutdated=True, isResolved=False, body="**P3**")]},
    )

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh, sync=True)

    # Assert
    record = outcomes[0].record
    assert any(action.mutation == "resolveReviewThread" and action.threadId == "PRRT_T1"
               for action in record.stage15_actions)
    # FakeGhClient recorded the call
    assert any(call[0] == "resolve_thread" for call in gh.calls)


def test_poll_sync_skips_resolve_when_github_already_resolved(store: StateStore) -> None:
    # Arrange — already resolved on GitHub
    gh = FakeGhClient(
        prs=[_pr_summary(ci=[{"name": "linux", "conclusion": "SUCCESS"}])],
        review_threads={49: [_codex_thread(thread_id="PRRT_T1", isOutdated=True, isResolved=True, body="**P3**")]},
    )

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh, sync=True)

    # Assert — no mutation invoked
    assert outcomes[0].record.stage15_actions == []
    assert not any(call[0] == "resolve_thread" for call in gh.calls)


def test_poll_sync_skips_when_local_verdict_is_open(store: StateStore) -> None:
    # Arrange — fresh thread (state A → OPEN), GitHub also unresolved
    gh = FakeGhClient(
        prs=[_pr_summary(ci=[{"name": "linux", "conclusion": "SUCCESS"}])],
        review_threads={49: [_codex_thread(body="**P3**")]},
    )

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh, sync=True)

    # Assert — local verdict is OPEN; sync MUST NOT fire (Stage 1.5 guardrail)
    assert outcomes[0].record.stage15_actions == []
    assert not any(call[0] == "resolve_thread" for call in gh.calls)


def test_poll_blocked_when_ci_failure(store: StateStore) -> None:
    # Arrange
    gh = FakeGhClient(prs=[_pr_summary(ci=[{"name": "linux", "conclusion": "FAILURE"}])])

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert
    assert outcomes[0].record.status is Status.BLOCKED


def test_poll_pending_when_ci_in_progress(store: StateStore) -> None:
    # Arrange
    gh = FakeGhClient(prs=[_pr_summary(ci=[{"name": "linux", "conclusion": "IN_PROGRESS"}])])

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert
    assert outcomes[0].record.status is Status.PENDING


# --- CHG-1107: poll short-circuit (state_key comparison) -------------------


def test_poll_appends_new_record_even_when_state_unchanged(store: StateStore) -> None:
    """CHG-1107 Test A: second poll with same state_key still appends to JSONL
    (audit trail unbroken), and sets is_no_change=True on the second outcome."""
    # Arrange — one ready PR with green CI, no codex threads
    gh = FakeGhClient(prs=[_pr_summary(ci=[{"name": "ci", "conclusion": "SUCCESS"}])])

    # Act — first poll
    outcomes1 = poll("owner/repo", store=store, gh_client=gh)
    # Act — second poll (same gh client → same state)
    outcomes2 = poll("owner/repo", store=store, gh_client=gh)

    # Assert — two records persisted (append-only, never skip)
    persisted = list(store.read_polls())
    assert len(persisted) == 2

    # First observation: is_no_change is False (no prior to compare against)
    assert outcomes1[0].is_no_change is False
    # Second observation: same state_key → is_no_change is True
    assert outcomes2[0].is_no_change is True


@pytest.mark.parametrize("label,second_prs,second_threads", [
    (
        "head_sha_changed",
        [_pr_summary(head="def456", ci=[{"name": "ci", "conclusion": "SUCCESS"}])],
        None,
    ),
    (
        "ci_changed",
        [_pr_summary(head="abc123", ci=[{"name": "ci", "conclusion": "FAILURE"}])],
        None,
    ),
    (
        "codex_open_changed",
        [_pr_summary(head="abc123", ci=[{"name": "ci", "conclusion": "SUCCESS"}])],
        {49: [_codex_thread(body="**P3** style nit")]},
    ),
    (
        "status_changed",
        # CI in_progress → status goes from READY to PENDING
        [_pr_summary(head="abc123", ci=[{"name": "ci", "conclusion": "IN_PROGRESS"}])],
        None,
    ),
])
def test_poll_marks_no_change_only_when_state_key_matches(
    store: StateStore, label, second_prs, second_threads,
) -> None:
    """CHG-1107 Test B (parametrized): when any dimension of state_key changes,
    the second poll must have is_no_change=False. Plus one non-parametrized
    case where everything stays the same → is_no_change=True."""
    # First poll — ready PR with green CI, no threads
    gh1 = FakeGhClient(prs=[_pr_summary(ci=[{"name": "ci", "conclusion": "SUCCESS"}])])
    outcomes1 = poll("owner/repo", store=store, gh_client=gh1)
    assert outcomes1[0].is_no_change is False

    # Second poll — state_key changes in one dimension
    gh2_kwargs = {"prs": second_prs}
    if second_threads is not None:
        gh2_kwargs["review_threads"] = second_threads
    gh2 = FakeGhClient(**gh2_kwargs)
    outcomes2 = poll("owner/repo", store=store, gh_client=gh2)

    # State changed → must NOT be marked as no-change
    assert outcomes2[0].is_no_change is False


def test_poll_no_change_true_when_state_key_identical(store: StateStore) -> None:
    """CHG-1107 Test B companion: when state_key is identical, is_no_change=True."""
    gh = FakeGhClient(prs=[_pr_summary(ci=[{"name": "ci", "conclusion": "SUCCESS"}])])
    poll("owner/repo", store=store, gh_client=gh)
    outcomes2 = poll("owner/repo", store=store, gh_client=gh)
    assert outcomes2[0].is_no_change is True


# --- (original tests continue) ---


def test_poll_writes_thread_snapshots_for_each_codex_thread(store: StateStore) -> None:
    # Arrange
    gh = FakeGhClient(
        prs=[_pr_summary()],
        review_threads={49: [
            _codex_thread(thread_id="T1"),
            _codex_thread(thread_id="T2", body="**P3** another nit"),
        ]},
    )

    # Act
    outcomes = poll("owner/repo", store=store, gh_client=gh)

    # Assert
    assert {snap.thread_id for snap in outcomes[0].snapshots} == {"T1", "T2"}
    assert store.read_thread("owner/repo", 49, "T1") is not None
    assert store.read_thread("owner/repo", 49, "T2") is not None


def test_poll_outcome_is_changed_when_sync_actions_occur(store, monkeypatch, ready_poll):
    """P2 fix (Codex finding 3207007032): sync mutations are state changes.
    With --sync, a poll can resolve threads on GitHub even when state_key matches —
    that's a real mutation and quiet-mode consumers must NOT treat it as no-change.
    """
    from tests.conftest import FakeGhClient
    from swm.models import Stage15Action
    prior = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 49})
    store.append_poll(prior)
    # Build the SAME state_key in the next poll, but with a sync action present.
    monkeypatch.setattr(
        "swm.poll._maybe_sync",
        lambda *a, **kw: [Stage15Action(mutation="resolveReviewThread", threadId="T_x", result={"isResolved": True})],
    )
    fake = FakeGhClient(prs=[{
        "number": 49, "title": prior.title, "headRefOid": prior.head_sha,
        "baseRefName": "main", "isDraft": False, "mergeStateStatus": prior.merge_state,
        "statusCheckRollup": [{"name": k, "conclusion": v.value} for k, v in prior.ci.items()],
        "updatedAt": "2026-05-08T00:00:00Z",
    }])
    outcomes = poll(prior.repo, store=store, gh_client=fake, sync=True, base="main")
    assert len(outcomes) == 1
    assert outcomes[0].sync_actions, "sync action must be present"
    assert outcomes[0].is_no_change is False, (
        "P2: sync mutation must mark outcome as changed, not no-change"
    )


# --- CHG-1112: notify-on-positive-transition implementer check -------------


def test_swm_poll_emits_at_most_one_notify_line_per_invocation(
    store: StateStore, monkeypatch,
) -> None:
    """CHG-1112 implementer check (mirrors CHG-1107 Test C style).

    Mimics the cron-grep contract: `grep -c "^notify:"` must return
    * exactly 1 on a transition fixture (pending → ready), and
    * exactly 0 on a no-change fixture (re-poll at same state_key).

    Distinct prefix from `no change:` so a downstream cron pipeline can
    `grep -q "^notify:"` to surface only true positive transitions.
    """
    from typer.testing import CliRunner

    from swm.cli import app

    runner = CliRunner()
    from swm.models import CIConclusion, PollRecord

    # --- transition fixture: prior PENDING → new READY at new head ---------
    prior = PollRecord(
        ts=datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc),
        repo="owner/repo",
        pr=49,
        title="ci: skip docs",
        head_sha="9" * 40,
        status=Status.PENDING,
        ci={"ci": CIConclusion.IN_PROGRESS},
        merge_state="CLEAN",
        codex_open=0,
        codex_resolved=0,
        threads=[],
        trigger="seed",
    )
    store.append_poll(prior)

    fake_transition = FakeGhClient(prs=[_pr_summary(
        head="abc123",  # new head_sha
        ci=[{"name": "ci", "conclusion": "SUCCESS"}],  # now green → READY
    )])
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake_transition)

    result_transition = runner.invoke(
        app, ["poll", "owner/repo", "--state-dir", str(store.directory)],
    )
    assert result_transition.exit_code == 0, result_transition.stdout

    notify_lines = [
        line for line in result_transition.stdout.splitlines()
        if line.startswith("notify:")
    ]
    assert len(notify_lines) == 1, (
        f"CHG-1112: pending→ready transition must emit exactly one '^notify:' "
        f"line. Got {len(notify_lines)}:\n{result_transition.stdout}"
    )

    # --- no-change fixture: same FakeGhClient, second invocation -----------
    # State_key now matches the just-persisted READY record — CHG-1107
    # short-circuit fires BEFORE the notify branch.
    result_no_change = runner.invoke(
        app, ["poll", "owner/repo", "--state-dir", str(store.directory)],
    )
    assert result_no_change.exit_code == 0, result_no_change.stdout

    notify_lines_2 = [
        line for line in result_no_change.stdout.splitlines()
        if line.startswith("notify:")
    ]
    assert len(notify_lines_2) == 0, (
        f"CHG-1112: no-change re-poll must emit zero '^notify:' lines "
        f"(CHG-1107 short-circuit fires first). Got {len(notify_lines_2)}:\n"
        f"{result_no_change.stdout}"
    )
    assert "no change:" in result_no_change.stdout


def test_poll_investigator_falls_back_on_subprocess_timeout(store: StateStore) -> None:
    """subprocess.TimeoutExpired must be caught and treated as an investigation error,
    not propagate and abort the poll."""
    import subprocess as _subprocess
    from swm.investigator import InvestigationError

    class TimeoutInvestigator:
        def investigate(self, item):
            raise _subprocess.TimeoutExpired(cmd="openclawcli", timeout=30)

    gh = FakeGhClient(
        prs=[_pr_summary()],
        review_threads={49: [_codex_thread(thread_id="PRRT_timeout")]},
    )

    outcomes = poll("owner/repo", store=store, gh_client=gh, investigator=TimeoutInvestigator())

    record = outcomes[0].record
    # Poll must complete (not raise); deterministic heuristic takes over.
    assert record is not None
    thread = record.threads[0]
    assert thread.llm_verdict is None
    assert thread.llm_reason is None

def test_poll_continues_when_diff_fetch_fails(store: StateStore) -> None:
    """A GhCommandError from pr_diff must not abort the poll; llm_error records the failure."""
    from swm.gh import GhCommandError
    from swm.investigator import InvestigationDecision

    class AlwaysResolveInvestigator:
        def investigate(self, item):
            return InvestigationDecision(verdict="RESOLVED", confidence=0.9, reason="ok", evidence=[])

    class DiffFailingGh(FakeGhClient):
        def pr_diff(self, repo, pr):
            raise GhCommandError("simulated diff fetch failure")

    gh = DiffFailingGh(
        prs=[_pr_summary()],
        review_threads={49: [_codex_thread(thread_id="PRRT_diff_fail", isOutdated=False)]},
    )

    outcomes = poll("owner/repo", store=store, gh_client=gh, investigator=AlwaysResolveInvestigator())

    record = outcomes[0].record
    assert record is not None, "poll must not abort on diff fetch failure"
    # Evidence must record the diff error so it is auditable.
    snap = store.read_thread("owner/repo", 49, "PRRT_diff_fail")
    assert snap is not None
    assert snap.evidence is not None
    assert snap.evidence.llm_error is not None
    assert "diff fetch failed" in snap.evidence.llm_error


def test_poll_skips_llm_when_diff_fetch_fails(store: StateStore) -> None:
    """When pr_diff raises, the investigator must not be called — the model
    would receive an empty diff and could return RESOLVED on thin evidence,
    potentially closing threads and making the PR ready incorrectly."""
    from swm.gh import GhCommandError
    from swm.investigator import InvestigationDecision

    investigate_calls: list[int] = []

    class TrackingInvestigator:
        def investigate(self, item):
            investigate_calls.append(1)
            return InvestigationDecision(verdict="RESOLVED", confidence=0.9, reason="ok", evidence=[])

    class DiffFailingGh(FakeGhClient):
        def pr_diff(self, repo, pr):
            raise GhCommandError("simulated diff fetch failure")

    gh = DiffFailingGh(
        prs=[_pr_summary()],
        review_threads={49: [_codex_thread(thread_id="PRRT_skip_llm", isOutdated=False)]},
    )

    outcomes = poll("owner/repo", store=store, gh_client=gh, investigator=TrackingInvestigator())

    assert len(investigate_calls) == 0, (
        "investigator.investigate() must not be called when diff fetch fails; "
        f"was called {len(investigate_calls)} time(s)"
    )
    # Thread verdict must come from deterministic heuristic, not LLM.
    snap = store.read_thread("owner/repo", 49, "PRRT_skip_llm")
    assert snap is not None and snap.evidence is not None
    assert snap.evidence.llm_evidence is None or snap.evidence.llm_evidence == []

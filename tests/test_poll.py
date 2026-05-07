"""Integration tests for the poll orchestrator — uses FakeGhClient (conftest)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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

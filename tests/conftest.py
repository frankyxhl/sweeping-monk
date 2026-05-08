"""Shared fixtures for unit + BDD tests."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from swm.gh import GhClient
from swm.models import (
    CIConclusion,
    Evidence,
    GitHubThreadState,
    PollRecord,
    Severity,
    Stage15Action,
    Status,
    Thread,
    ThreadSnapshot,
    Verdict,
    VerdictHistoryEntry,
)
from swm.state import StateStore

REPO = "owner/repo"
PR = 49
THREAD_ID = "PRRT_test_thread_1"


def _ts(hour: int, minute: int) -> datetime:
    return datetime(2026, 5, 7, hour, minute, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    """A fresh StateStore rooted at a tmp dir — never touches the real state/."""
    return StateStore(tmp_path / "state")


@pytest.fixture
def open_thread() -> Thread:
    """Codex P2 finding with no author response yet — verdict OPEN."""
    return Thread(
        id=THREAD_ID,
        comment_id=1001,
        path=".github/workflows/test.yml",
        line=20,
        codex_severity=Severity.P2,
        effective_severity=Severity.P3,
        verdict=Verdict.OPEN,
        title="Keep required Test checks satisfiable",
        verdict_reason="no author response yet",
        github_isResolved=False,
    )


@pytest.fixture
def resolved_thread() -> Thread:
    """Same finding but after Stage 1.5 sync — verdict RESOLVED on GitHub too."""
    return Thread(
        id=THREAD_ID,
        comment_id=1001,
        path=".github/workflows/test.yml",
        line=31,
        codex_severity=Severity.P2,
        effective_severity=Severity.P3,
        verdict=Verdict.RESOLVED,
        title="Keep required Test checks satisfiable",
        verdict_reason="State C; author reply substantive",
        github_isResolved=True,
        author_reply_id=2002,
        author_reply_substantive=True,
        code_changed=True,
        new_commit_sha="abc12345",
        github_resolvedBy="frankyxhl",
        stage15_synced_at=_ts(12, 54),
        demotion_reason="main has no branch protection",
    )


@pytest.fixture
def pending_poll(open_thread: Thread) -> PollRecord:
    return PollRecord(
        ts=_ts(12, 41),
        repo=REPO,
        pr=PR,
        title="ci: skip Test workflow on docs-only PRs",
        head_sha="f895210fee" + "0" * 30,
        status=Status.PENDING,
        ci={"ubuntu-latest": CIConclusion.SUCCESS, "macos-latest": CIConclusion.IN_PROGRESS},
        merge_state="UNSTABLE",
        codex_open=1,
        codex_resolved=0,
        codex_last_review_at=_ts(12, 41),
        codex_last_review_head="f895210fee" + "0" * 30,
        threads=[open_thread],
        summary="initial scan",
        trigger="initial-scan",
    )


@pytest.fixture
def ready_poll(resolved_thread: Thread) -> PollRecord:
    return PollRecord(
        ts=_ts(12, 54),
        repo=REPO,
        pr=PR,
        title="ci: skip Test workflow on docs-only PRs",
        head_sha="c476c877b5" + "0" * 30,
        status=Status.READY,
        ci={"ubuntu-latest": CIConclusion.SUCCESS, "macos-latest": CIConclusion.SUCCESS},
        merge_state="CLEAN",
        codex_open=0,
        codex_resolved=1,
        codex_last_review_at=_ts(12, 41),
        codex_last_review_head="f895210fee" + "0" * 30,
        threads=[resolved_thread],
        summary="Stage 1.5 sync resolved thread on GitHub",
        trigger="stage1.5-sync",
        stage15_actions=[Stage15Action(
            mutation="resolveReviewThread",
            threadId=THREAD_ID,
            result={"isResolved": True, "resolvedBy": "frankyxhl"},
        )],
    )


class FakeGhClient(GhClient):
    """In-memory GhClient stub. Tests pre-populate responses; mutations land in `calls`."""

    def __init__(self, *, prs: list[dict] | None = None,
                 review_threads: dict[int, list[dict]] | None = None,
                 branch_protection_data: dict | None = None,
                 pr_body_reactions: dict[int, list[dict]] | None = None,
                 active_login: str = "ryosaeba1985",
                 pr_bodies: dict[int, str] | None = None,
                 review_should_fail: bool = False,
                 edit_should_fail: bool = False) -> None:
        # Skip parent __init__ — we override every public method.
        self._prs = prs or []
        self._review_threads = review_threads or {}
        self._branch_protection = branch_protection_data
        self._pr_body_reactions = pr_body_reactions or {}
        self._active_login = active_login
        self._pr_bodies = pr_bodies or {}
        self._review_should_fail = review_should_fail
        self._edit_should_fail = edit_should_fail
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def list_open_prs(self, repo: str, *, base: str | None = None) -> list[dict]:
        self._record("list_open_prs", repo, base=base)
        return list(self._prs)

    def view_pr(self, repo: str, pr: int, fields: list[str]) -> dict:
        self._record("view_pr", repo, pr, fields=fields)
        base = next((p for p in self._prs if p.get("number") == pr), {})
        if pr in self._pr_bodies:
            base = {**base, "body": self._pr_bodies[pr]}
        return base

    def pulls_comments(self, repo: str, pr: int) -> list[dict]:
        self._record("pulls_comments", repo, pr)
        return []

    def issues_comments(self, repo: str, pr: int) -> list[dict]:
        self._record("issues_comments", repo, pr)
        return []

    def branch_protection(self, repo: str, branch: str) -> dict | None:
        self._record("branch_protection", repo, branch)
        return self._branch_protection

    def review_threads(self, repo: str, pr: int) -> list[dict]:
        self._record("review_threads", repo, pr)
        return list(self._review_threads.get(pr, []))

    def pr_body_reactions(self, repo: str, pr: int) -> list[dict]:
        self._record("pr_body_reactions", repo, pr)
        return list(self._pr_body_reactions.get(pr, []))

    def resolve_thread(self, thread_id: str) -> dict:
        self._record("resolve_thread", thread_id)
        return {"id": thread_id, "isResolved": True, "resolvedBy": {"login": "tester"}}

    def unresolve_thread(self, thread_id: str) -> dict:
        self._record("unresolve_thread", thread_id)
        return {"id": thread_id, "isResolved": False}

    def auth_active_login(self) -> str:
        self._record("auth_active_login")
        return self._active_login

    def submit_review_approve(self, repo: str, pr: int, body: str) -> dict:
        self._record("submit_review_approve", repo, pr, body=body)
        if self._review_should_fail:
            from swm.gh import GhCommandError
            raise GhCommandError("simulated review failure")
        return {"stdout": "approved"}

    def edit_pr_body(self, repo: str, pr: int, body: str) -> dict:
        self._record("edit_pr_body", repo, pr, body=body)
        if self._edit_should_fail:
            from swm.gh import GhCommandError
            raise GhCommandError("simulated edit failure")
        # Mirror the edit into pr_bodies so subsequent view_pr sees the new body.
        self._pr_bodies[pr] = body
        return {"stdout": "edited"}


@pytest.fixture
def thread_snapshot() -> ThreadSnapshot:
    return ThreadSnapshot(
        thread_id=THREAD_ID,
        repo=REPO,
        pr=PR,
        first_seen=_ts(12, 41),
        last_polled=_ts(12, 54),
        codex_comment_id=1001,
        path=".github/workflows/test.yml",
        current_line=31,
        original_line=20,
        codex_severity=Severity.P2,
        effective_severity=Severity.P3,
        demotion_reason="main has no branch protection",
        verdict=Verdict.RESOLVED,
        verdict_history=[
            VerdictHistoryEntry(ts=_ts(12, 41), verdict=Verdict.OPEN, reason="no author response"),
            VerdictHistoryEntry(ts=_ts(12, 54), verdict=Verdict.RESOLVED, reason="author reply substantive"),
        ],
        evidence=Evidence(
            thread_state="C",
            author_reply_id=2002,
            author_reply_substantive=True,
            author_reply_summary="cites gh api proof",
            code_changed=True,
            code_change_commit="abc12345",
            code_change_summary="added 11-line FOOT-GUN comment",
            codex_followed_up=False,
            demotion_reason="main has no branch protection",
            synced_via="Stage 1.5 resolveReviewThread mutation",
            synced_at=_ts(12, 54),
        ),
        github_state=GitHubThreadState(
            isResolved=True,
            isOutdated=False,
            resolvedBy="frankyxhl",
            synced_at=_ts(12, 54),
        ),
    )

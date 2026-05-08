"""Pydantic models for watchdog state.

Mirrors the JSON shapes documented in CLAUDE.md (Output Format) and SWM-1101
(thread verdict shape). Validation here is the single source of truth — both
JSONL writes and reads route through these models.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Status(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    PENDING = "pending"
    ERROR = "error"
    SKIPPED = "skipped"


class Verdict(str, Enum):
    RESOLVED = "RESOLVED"
    OPEN = "OPEN"
    NEEDS_HUMAN_JUDGMENT = "NEEDS_HUMAN_JUDGMENT"


class Severity(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class CIConclusion(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING = "PENDING"
    SKIPPED = "SKIPPED"
    NEUTRAL = "NEUTRAL"
    CANCELLED = "CANCELLED"


class Stage15Action(BaseModel):
    """Record of a Stage 1.5 GraphQL mutation the watchdog performed."""
    mutation: Literal["resolveReviewThread", "unresolveReviewThread"]
    threadId: str
    result: dict


class Thread(BaseModel):
    """One Codex review thread on a PR. Embedded in PollRecord and (more fully) in ThreadSnapshot."""
    model_config = ConfigDict(extra="allow")

    id: str
    comment_id: int
    path: str
    line: int | None = None
    codex_severity: Severity
    effective_severity: Severity
    verdict: Verdict
    title: str | None = None
    verdict_reason: str | None = None
    github_isResolved: bool = False
    # Optional fields that appear once an author has responded:
    author_reply_id: int | None = None
    author_reply_substantive: bool | None = None
    code_changed: bool | None = None
    new_commit_sha: str | None = None
    demotion_reason: str | None = None
    github_resolvedBy: str | None = None
    stage15_synced_at: datetime | None = None


class PollRecord(BaseModel):
    """One snapshot in state/polls.jsonl. Append-only; never mutated."""
    model_config = ConfigDict(extra="allow")

    ts: datetime
    repo: str
    pr: int
    title: str | None = None
    head_sha: str
    status: Status
    ci: dict[str, CIConclusion] = Field(default_factory=dict)
    merge_state: str | None = None
    codex_open: int = 0
    codex_resolved: int = 0
    codex_last_review_at: datetime | None = None
    codex_last_review_head: str | None = None
    codex_pr_body_signal: Literal["reviewing", "approved"] | None = None
    threads: list[Thread] = Field(default_factory=list)
    summary: str | None = None
    trigger: str | None = None
    stage15_actions: list[Stage15Action] = Field(default_factory=list)

    def state_key(self) -> tuple:
        """Comparison key per SWM-1100 step 3 — short-circuit when unchanged."""
        return (
            self.pr,
            self.head_sha,
            tuple(sorted((k, v.value) for k, v in self.ci.items())),
            self.codex_open,
            self.status.value,
        )


class VerdictHistoryEntry(BaseModel):
    ts: datetime
    verdict: Verdict
    reason: str


class Evidence(BaseModel):
    """Evidence chain backing a thread verdict — what the watchdog observed."""
    model_config = ConfigDict(extra="allow")

    thread_state: Literal["A", "B", "C"] | None = None
    author_reply_id: int | None = None
    author_reply_substantive: bool | None = None
    author_reply_summary: str | None = None
    code_changed: bool | None = None
    code_change_commit: str | None = None
    code_change_summary: str | None = None
    codex_followed_up: bool | None = None
    codex_reaction: str | None = None
    demotion_reason: str | None = None
    synced_via: str | None = None
    synced_at: datetime | None = None


class GitHubThreadState(BaseModel):
    isResolved: bool
    isOutdated: bool = False
    resolvedBy: str | None = None
    synced_via: str | None = None
    synced_at: datetime | None = None


class BoxMiss(BaseModel):
    """One skipped-box observation from `swm tick`. Append-only.

    Distinct from LedgerEntry: misses are observations of what the classifier
    saw, not authorized actions. No actor, no authorization, no result — just
    the box text + the rule's verdict + a reason. Feeds the `swm rule-coverage`
    report (CHG-1105) so blind spots become visible without the maintainer
    having to push back on every PR.
    """
    model_config = ConfigDict(extra="allow")

    ts: datetime
    repo: str
    pr: int
    head_sha: str
    box_text: str
    rule_id: str | None = None
    satisfied: bool = False
    reason: str


class LedgerAction(str, Enum):
    SUBMIT_REVIEW_APPROVE = "submit_review_approve"
    EDIT_PR_BODY_CHECK_BOXES = "edit_pr_body_check_boxes"


class LedgerEntry(BaseModel):
    """One Stage-3+ write the watchdog made under SWM-1103 authorization.

    Append-only — never mutated. Older hand-written entries in `ledger.jsonl`
    may carry extra top-level fields; `extra="allow"` keeps them readable.
    """
    model_config = ConfigDict(extra="allow")

    ts: datetime
    repo: str
    pr: int
    head_sha: str
    action: LedgerAction
    actor: str
    authorized_by: str
    reason: str
    evidence: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)


class ThreadSnapshot(BaseModel):
    """One thread's full living state in state/threads/<id>.json — overwritten on each poll."""
    model_config = ConfigDict(extra="allow")

    thread_id: str
    repo: str
    pr: int
    first_seen: datetime
    last_polled: datetime
    codex_comment_id: int
    path: str
    current_line: int | None = None
    original_line: int | None = None
    codex_severity: Severity
    effective_severity: Severity
    demotion_reason: str | None = None
    verdict: Verdict
    verdict_history: list[VerdictHistoryEntry] = Field(default_factory=list)
    evidence: Evidence = Field(default_factory=Evidence)
    github_state: GitHubThreadState | None = None

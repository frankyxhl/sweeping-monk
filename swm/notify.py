"""CHG-1112: positive ready/approved transition detector for `swm poll`.

Runs AFTER the CHG-1107 short-circuit, so identical `state_key` polls never
reach it. Every dimension this module branches on MUST also live in
`PollRecord.state_key()` — otherwise the short-circuit returns first and the
branch becomes dead code (round-1 trap, see SWM-1112 §What invariant).
"""
from __future__ import annotations

import shlex
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .models import CIConclusion, PollRecord, Status
from .state import now_utc

Transition = Literal[
    "first-ready",
    "blocked-to-ready",
    "pending-to-ready",
    "ready-after-head-bump",
]


class NotificationRecord(BaseModel):
    """One row in `state/notifications.jsonl`. Append-only."""
    model_config = ConfigDict(extra="forbid")

    ts: datetime
    repo: str
    pr: int
    title: str | None
    head_sha: str
    transition: Transition
    suggested_action: str
    summary: str

    @classmethod
    def from_transition(
        cls, prior: PollRecord | None, new: PollRecord, transition: Transition,
    ) -> "NotificationRecord":
        """Build a NotificationRecord. Summary is deterministic, ≤ 200 chars."""
        prior_status = prior.status.value if prior is not None else "none"
        prior_head = prior.head_sha[:8] if prior is not None else "none"
        summary = (
            f"{transition}: {prior_status}@{prior_head} -> "
            f"{new.status.value}@{new.head_sha[:8]}; codex_open={new.codex_open}"
        )[:200]
        reason = f"{transition}: {_describe_ci(new.ci)}, no blocking findings"
        return cls(
            ts=now_utc(), repo=new.repo, pr=new.pr, title=new.title,
            head_sha=new.head_sha, transition=transition,
            suggested_action=format_suggested_action(
                repo=new.repo, pr=new.pr, reason=reason, title=new.title,
            ),
            summary=summary,
        )


def detect_positive_transition(
    prior: PollRecord | None, new: PollRecord,
) -> Transition | None:
    """Branch order is load-bearing — see test_blocked_to_ready_at_new_head."""
    if new.status != Status.READY:
        return None
    if prior is None:
        return "first-ready"
    if prior.status == Status.BLOCKED:
        return "blocked-to-ready"
    if prior.status == Status.PENDING:
        return "pending-to-ready"
    # Codex PR #11 #3212804778: head-bump branch is READY→READY only.
    # ERROR/SKIPPED → READY is a recovery transition; defer to its own CHG
    # rather than mis-fire as `ready-after-head-bump` (CHG-1112 §Out-of-scope).
    if prior.status == Status.READY and prior.head_sha != new.head_sha:
        return "ready-after-head-bump"
    return None


def _describe_ci(ci: dict[str, CIConclusion]) -> str:
    """Honest CI evidence string for the approval reason (Codex PR #11
    review #3212833878). `_compute_status` returns READY for empty-CI PRs
    (paths-ignore / docs-only); never claim "ci green" in that case."""
    if not ci:
        return "no required CI configured"
    if all(c == CIConclusion.SUCCESS for c in ci.values()):
        return f"ci green ({len(ci)} checks)"
    return f"ci mixed ({len(ci)} checks)"


def format_suggested_action(
    *, repo: str, pr: int, reason: str, title: str | None = None,
) -> str:
    """Shell-safe `swm approve` invocation. `title` accepted but NEVER embedded
    — PR titles live in `summary` (shell-safety boundary)."""
    return f"swm approve {repo} {pr} --reason {shlex.quote(reason)}"

"""SWM-1103 guarded one-shot writes — pure logic.

Three gates expressed as pure functions over `GhClient` and `StateStore` data.
The CLI layer composes them; tests pin every branch in isolation.

Never makes network or filesystem mutations on its own — every mutation goes
through the CLI subcommand, which is responsible for the user-confirmation
prompt and for appending the ledger entry only after the gh call returns
success.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .gh import GhClient
from .models import BoxMiss, CIConclusion, LedgerEntry, PollRecord, Status
from .state import StateStore, now_utc

PREFERRED_AGENT_LOGIN = "ryosaeba1985"
CHECKBOX_RE = re.compile(r"^(\s*)-\s+\[([ xX])\]\s+(.+)$")
UNCHECKED_RE = re.compile(r"^(\s*)-\s+\[ \]\s+(.+)$")


# --- identity ---------------------------------------------------------------


@dataclass
class IdentityCheck:
    active_login: str
    pr_author: str
    is_self_action: bool
    is_preferred_identity: bool

    @property
    def can_proceed(self) -> bool:
        # GitHub blocks self-approval / self-edit-as-author for review submissions;
        # this mirrors that hard rule. PR-body edits to one's own PR are technically
        # allowed by GitHub, but SWM-1103 still wants a non-author actor for audit.
        return not self.is_self_action

    @property
    def blocker(self) -> str | None:
        if self.is_self_action:
            return f"identity blocked: active gh account {self.active_login!r} is the PR author — GitHub blocks self-approval"
        if not self.is_preferred_identity:
            return None  # warning only, not a blocker
        return None


def check_identity(gh: GhClient, repo: str, pr: int) -> IdentityCheck:
    """Read `gh auth status` + PR author. Pure data classification."""
    active = gh.auth_active_login()
    view = gh.view_pr(repo, pr, ["author"])
    author = (view.get("author") or {}).get("login", "")
    return IdentityCheck(
        active_login=active,
        pr_author=author,
        is_self_action=bool(active and author and active == author),
        is_preferred_identity=(active == PREFERRED_AGENT_LOGIN),
    )


# --- verdict / freshness ----------------------------------------------------


@dataclass
class VerdictCheck:
    has_poll: bool
    poll_status: Status | None
    poll_head_sha: str | None
    current_head_sha: str
    head_sha_fresh: bool

    def supports_approve(self) -> tuple[bool, str | None]:
        if not self.has_poll:
            return False, "no recorded poll for this PR — run `swm poll` first"
        if self.poll_status != Status.READY:
            return False, f"latest poll status is {self.poll_status.value if self.poll_status else 'unknown'}, not ready"
        if not self.head_sha_fresh:
            return False, (
                f"verdict was computed against head {self.poll_head_sha[:8] if self.poll_head_sha else '?'}, "
                f"but current head is {self.current_head_sha[:8]} — re-poll first"
            )
        return True, None

    def supports_tick(self) -> tuple[bool, str | None]:
        if not self.has_poll:
            return False, "no recorded poll for this PR — run `swm poll` first"
        if not self.head_sha_fresh:
            return False, (
                f"verdict was computed against head {self.poll_head_sha[:8] if self.poll_head_sha else '?'}, "
                f"but current head is {self.current_head_sha[:8]} — re-poll first"
            )
        return True, None


def check_verdict(store: StateStore, repo: str, pr: int, current_head_sha: str) -> VerdictCheck:
    poll = store.latest_poll(repo, pr)
    if poll is None:
        return VerdictCheck(False, None, None, current_head_sha, False)
    return VerdictCheck(
        has_poll=True,
        poll_status=poll.status,
        poll_head_sha=poll.head_sha,
        current_head_sha=current_head_sha,
        head_sha_fresh=(poll.head_sha == current_head_sha),
    )


# --- approve body template --------------------------------------------------


def render_approve_body(
    poll: PollRecord, reason: str, *, actor_label: str = "maintainer",
) -> str:
    """Templated factual body — not free-form opinion. Stays Stage-3 acceptable."""
    ci_lines = []
    for runner, conclusion in sorted(poll.ci.items()):
        mark = "✓" if conclusion == CIConclusion.SUCCESS else "•"
        ci_lines.append(f"{mark} {runner}: {conclusion.value}")
    ci_block = "\n".join(ci_lines) if ci_lines else "(no CI runs reported)"
    codex_signal = poll.codex_pr_body_signal or "no PR-body reaction"
    return (
        f"Approved by {actor_label}. Reviewed via local watchdog (SWM-1103):\n"
        f"\n"
        f"- head: {poll.head_sha[:8]}\n"
        f"- CI:\n{ci_block}\n"
        f"- Codex bot: {codex_signal}\n"
        f"- inline findings: {poll.codex_open} open, {poll.codex_resolved} resolved\n"
        f"\n"
        f"Reason: {reason}"
    )


# --- checkbox parsing + classification --------------------------------------


@dataclass
class CheckboxLine:
    raw: str
    text: str          # everything after "- [ ] "
    line_number: int   # 1-based, in the PR body
    checked: bool = False


def parse_checkboxes(body: str) -> list[CheckboxLine]:
    out: list[CheckboxLine] = []
    for i, line in enumerate(body.splitlines(), start=1):
        m = CHECKBOX_RE.match(line)
        if m:
            out.append(
                CheckboxLine(
                    raw=line,
                    text=m.group(3),
                    line_number=i,
                    checked=m.group(2).lower() == "x",
                )
            )
    return out


def parse_unchecked_boxes(body: str) -> list[CheckboxLine]:
    return [box for box in parse_checkboxes(body) if not box.checked]


@dataclass
class BoxClassification:
    box: CheckboxLine
    rule_id: str | None     # which rule matched, or None when unverifiable
    satisfied: bool         # only meaningful when rule_id is not None
    reason: str             # human-readable evidence summary

    @property
    def should_flip(self) -> bool:
        return self.rule_id is not None and self.satisfied


# Predicate: (poll) -> (satisfied, reason)
Predicate = Callable[[PollRecord], tuple[bool, str]]


def _ci_runner_predicate(needle: str) -> Predicate:
    """Match any CI key whose name contains `needle` (case-insensitive).

    Three branches:
    - poll.ci has matching runner(s) all SUCCESS → satisfied.
    - poll.ci has matching runner(s) but not all green → not satisfied.
    - poll.ci is empty AND poll.status == READY → satisfied (paths-ignore /
      docs-only PR; the parent verdict has already trusted the empty-CI state
      after the CI grace window in poll.py). This trust transfer is the only
      way an unsatisfiable predicate becomes satisfied — if status is anything
      other than READY, an empty CI dict is still treated as unverified.
    - poll.ci is non-empty but no runner matches → not satisfied (the runner
      that should have fired didn't — suspicious, leave to maintainer).
    """
    def _check(poll: PollRecord) -> tuple[bool, str]:
        if not poll.ci:
            if poll.status == Status.READY:
                return True, f"no CI runs (paths-ignore / docs-only); parent verdict=ready"
            return False, f"no CI runs and parent verdict={poll.status.value} (not yet trusted)"
        matches = [(k, v) for k, v in poll.ci.items() if needle.lower() in k.lower()]
        if not matches:
            return False, f"no CI runner matching {needle!r}"
        bad = [k for k, v in matches if v != CIConclusion.SUCCESS]
        if bad:
            return False, f"runner(s) not green: {', '.join(bad)}"
        return True, f"{', '.join(k for k, _ in matches)} = SUCCESS"
    return _check


def _all_ci_green(poll: PollRecord) -> tuple[bool, str]:
    if not poll.ci:
        if poll.status == Status.READY:
            return True, "no CI runs (paths-ignore / docs-only); parent verdict=ready"
        return False, f"no CI runs and parent verdict={poll.status.value} (not yet trusted)"
    bad = [k for k, v in poll.ci.items() if v != CIConclusion.SUCCESS]
    if bad:
        return False, f"non-green runners: {', '.join(bad)}"
    return True, f"all {len(poll.ci)} CI runners SUCCESS"


def _codex_approved(poll: PollRecord) -> tuple[bool, str]:
    if poll.codex_pr_body_signal == "approved":
        return True, "codex_pr_body_signal=approved"
    return False, f"codex_pr_body_signal={poll.codex_pr_body_signal!r}"


# Order matters: more specific patterns first.
BOX_RULES: list[tuple[str, re.Pattern[str], Predicate]] = [
    ("ci.ubuntu", re.compile(r"\bCI\s+ubuntu(?:-latest)?\s+passes?\b", re.IGNORECASE), _ci_runner_predicate("ubuntu")),
    ("ci.macos", re.compile(r"\bCI\s+macos(?:-latest)?\s+passes?\b", re.IGNORECASE), _ci_runner_predicate("macos")),
    ("ci.both", re.compile(r"\bCI\s+ubuntu[^\n]*macos\b", re.IGNORECASE), _all_ci_green),
    ("codex.review", re.compile(r"\bCodex\b.*\b(GitHub\s+)?bot\s+review\b", re.IGNORECASE), _codex_approved),
]


def classify_box(box: CheckboxLine, poll: PollRecord) -> BoxClassification:
    for rule_id, pattern, predicate in BOX_RULES:
        if pattern.search(box.text):
            satisfied, reason = predicate(poll)
            return BoxClassification(box=box, rule_id=rule_id, satisfied=satisfied, reason=reason)
    return BoxClassification(box=box, rule_id=None, satisfied=False, reason="no rule matched — manual check required")


def apply_box_flips(body: str, line_numbers: list[int]) -> str:
    """Flip `- [ ]` to `- [x]` only at the listed 1-based line numbers. Preserves line endings."""
    targets = set(line_numbers)
    out_lines: list[str] = []
    for i, line in enumerate(body.splitlines(keepends=True), start=1):
        if i in targets and UNCHECKED_RE.match(line.rstrip("\r\n")):
            out_lines.append(line.replace("- [ ]", "- [x]", 1))
        else:
            out_lines.append(line)
    return "".join(out_lines)


# --- ledger helpers ---------------------------------------------------------


def build_approve_ledger_entry(
    *, poll: PollRecord, actor: str, reason: str, authorized_by: str,
    review_result: dict, ts: datetime | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        ts=ts or now_utc(),
        repo=poll.repo,
        pr=poll.pr,
        head_sha=poll.head_sha,
        action="submit_review_approve",
        actor=actor,
        authorized_by=authorized_by,
        reason=reason,
        evidence={
            "verdict": poll.status.value,
            "ci": {k: v.value for k, v in poll.ci.items()},
            "codex_pr_body_signal": poll.codex_pr_body_signal,
            "codex_open": poll.codex_open,
            "codex_resolved": poll.codex_resolved,
        },
        result=review_result,
    )


def build_box_miss(
    *, classification: BoxClassification, poll: PollRecord, ts: datetime | None = None,
) -> BoxMiss:
    return BoxMiss(
        ts=ts or now_utc(),
        repo=poll.repo,
        pr=poll.pr,
        head_sha=poll.head_sha,
        box_text=classification.box.text,
        rule_id=classification.rule_id,
        reason=classification.reason,
    )


def build_tick_ledger_entry(
    *, poll: PollRecord, actor: str, authorized_by: str, reason: str,
    flipped: list[BoxClassification], ts: datetime | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        ts=ts or now_utc(),
        repo=poll.repo,
        pr=poll.pr,
        head_sha=poll.head_sha,
        action="edit_pr_body_check_boxes",
        actor=actor,
        authorized_by=authorized_by,
        reason=reason,
        evidence={
            "boxes_flipped": [
                {"line": c.box.line_number, "text": c.box.text, "rule": c.rule_id, "evidence": c.reason}
                for c in flipped
            ],
        },
        result={"diff_lines_changed": len(flipped)},
    )

"""Verdict assignment per SWM-1101 (Decision Tree steps 3-6).

The 'substantively reasonable' heuristic deliberately stays conservative:
we only return RESOLVED when there is concrete evidence (specific identifier,
sufficient length, no obvious deflection pattern). Borderline cases collapse to
NEEDS_HUMAN_JUDGMENT so the maintainer sees them rather than a false RESOLVED.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .models import Verdict

ThreadState = Literal["A", "B", "C"]

_COMMIT_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_FILE_RE = re.compile(r"\b[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|rb|java|sh|yml|yaml|toml|md)\b")
_IDENTIFIER_RE = re.compile(r"`[^`\n]{2,}`|\b(?:gh|git|npm|cargo|make|pip|api|graphql|mutation)\b", re.I)
_DEFLECT_RE = re.compile(r"\b(thanks|won't fix|wontfix|ack|noted|will look)\b", re.I)


def is_substantive_reply(body: str | None) -> bool:
    """True when the reply (a) is at least ~50 chars, (b) names a concrete identifier,
    and (c) is not predominantly a deflection phrase.
    """
    if not body:
        return False
    text = body.strip()
    if len(text) < 50:
        return False
    has_identifier = bool(
        _COMMIT_SHA_RE.search(text)
        or _FILE_RE.search(text)
        or _IDENTIFIER_RE.search(text)
    )
    if not has_identifier:
        return False
    # If body is short AND the only matches are deflection words, treat as weak.
    if _DEFLECT_RE.search(text) and len(text) < 100 and not _COMMIT_SHA_RE.search(text):
        return False
    return True


def codex_followup_reaction(followup_body: str | None) -> str | None:
    """Detect 👍 / 👎 / textual approval signals in a Codex follow-up. Returns
    'positive' / 'negative' / None.
    """
    if not followup_body:
        return None
    text = followup_body.lower()
    if any(token in text for token in ["looks good", "no new issues", "addressed", "resolved", "👍"]):
        return "positive"
    if any(token in text for token in ["still", "not addressed", "concern remains", "👎"]):
        return "negative"
    return None


@dataclass(frozen=True)
class VerdictDecision:
    verdict: Verdict
    reason: str
    substantive: bool | None = None


def judge(
    *,
    classification: ThreadState,
    author_reply_body: str | None,
    code_changed: bool,
    codex_followup_body: str | None,
    github_isResolved: bool = False,
) -> VerdictDecision:
    """Apply SWM-1101 steps 3-6 in order, returning the final verdict.

    `github_isResolved` is the system-of-record fast-path: if GitHub says
    the thread is resolved (manual UI resolve, prior Stage 1.5 sync, or
    SWM-1103 maintainer override), trust it over the local classifier.
    Otherwise, step 6 (Codex follow-up) overrides steps 3-5.
    """
    # Step 0 — GitHub system-of-record override.
    if github_isResolved:
        return VerdictDecision(
            Verdict.RESOLVED,
            "GitHub reports isResolved=true (external resolve / Stage 1.5 sync / maintainer override)",
        )

    # Step 6 short-circuit — explicit Codex approval / rejection wins.
    reaction = codex_followup_reaction(codex_followup_body)
    if reaction == "positive":
        return VerdictDecision(Verdict.RESOLVED, "Codex follow-up signaled approval (step 6)")
    if reaction == "negative":
        return VerdictDecision(Verdict.OPEN, "Codex follow-up restated concern (step 6)")

    if classification == "B":
        # Step 3 — author pushed code that invalidated the diff anchor.
        if code_changed:
            return VerdictDecision(
                Verdict.RESOLVED,
                "thread outdated; author commit changed the lines Codex anchored to (SWM-1101 step 3)",
            )
        return VerdictDecision(
            Verdict.NEEDS_HUMAN_JUDGMENT,
            "thread marked outdated but no matching code change detected — manual review",
        )

    if classification == "C":
        # Step 4-5 — author replied; judge substance.
        substantive = is_substantive_reply(author_reply_body)
        if substantive:
            return VerdictDecision(
                Verdict.RESOLVED,
                "author reply substantive (cites concrete identifier, ≥50 chars) per SWM-1101 step 4-5",
                substantive=True,
            )
        return VerdictDecision(
            Verdict.OPEN,
            "author reply non-substantive or borderline; defer to maintainer",
            substantive=False,
        )

    # Step 5 fallthrough — A: no reply, no code change.
    return VerdictDecision(Verdict.OPEN, "no author response and no code change (SWM-1101 step 5)")

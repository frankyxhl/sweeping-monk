"""Codex thread state classification per SWM-1101 ('Thread State Classification').

A — fresh: not outdated, no author reply yet
B — outdated: author pushed code that invalidated the diff anchor
C — replied: not outdated, author posted an in-thread reply

Inputs come from the GraphQL `pullRequestReviewThread` shape returned by
gh.review_threads(). One thread = one Codex top-level comment + zero or more
follow-up comments (replies).

A thread can be both B and C in practice (author fixed code AND replied). This
module returns the dominant state — B if outdated, otherwise C, otherwise A —
and downstream callers can still inspect the full thread for additional signal.
"""
from __future__ import annotations

from typing import Literal

CODEX_BOT_LOGIN = "chatgpt-codex-connector"  # GraphQL omits the [bot] suffix
CODEX_BOT_LOGIN_REST = "chatgpt-codex-connector[bot]"  # REST API includes [bot] suffix
SWM_MARKER_PREFIX = "<!-- swm-"  # prefix for all SWM-generated conclusion/close-reason markers
ThreadState = Literal["A", "B", "C"]
CodexBodySignal = Literal["reviewing", "approved"]


def codex_pr_body_signal(reactions: list[dict]) -> CodexBodySignal | None:
    """Codex bot signals its review state by reacting to the PR body itself:
      EYES (👀)        — currently reviewing this head
      THUMBS_UP (👍)  — reviewed; approves / no new issues

    A single bot reactor; if both ever appear (transition window), THUMBS_UP wins.
    Returns None when Codex hasn't reacted yet (it hasn't engaged with the PR).
    """
    has_thumbs = False
    has_eyes = False
    for r in reactions or []:
        login = ((r.get("user") or {}).get("login") or "")
        if login not in (CODEX_BOT_LOGIN, CODEX_BOT_LOGIN_REST):
            continue
        if r.get("content") == "THUMBS_UP":
            has_thumbs = True
        elif r.get("content") == "EYES":
            has_eyes = True
    if has_thumbs:
        return "approved"
    if has_eyes:
        return "reviewing"
    return None


def _login(comment: dict) -> str | None:
    """`(comment.author or {}).login` defended against null author/comment."""
    return ((comment or {}).get("author") or {}).get("login")


def _comment_nodes(thread: dict) -> list[dict]:
    return (thread.get("comments") or {}).get("nodes") or []


def is_codex_thread(thread: dict) -> bool:
    """A thread is 'Codex' iff its first comment was authored by the Codex bot."""
    comments = _comment_nodes(thread)
    return bool(comments) and _login(comments[0]) == CODEX_BOT_LOGIN


def codex_comment_id(thread: dict) -> int | None:
    comments = _comment_nodes(thread)
    return comments[0].get("databaseId") if comments else None


def author_replies(thread: dict) -> list[dict]:
    """All comments after the first one (Codex's) — i.e., replies."""
    return [c for c in _comment_nodes(thread)[1:] if c]


def latest_author_reply(thread: dict) -> dict | None:
    """The most recent non-Codex, non-SWM-marker reply, or None."""
    replies = [
        c for c in author_replies(thread)
        if _login(c) != CODEX_BOT_LOGIN
        and not (c.get("body") or "").startswith(SWM_MARKER_PREFIX)
    ]
    return replies[-1] if replies else None


def latest_codex_followup(thread: dict) -> dict | None:
    """A Codex follow-up comment after its initial review — used for 👍/👎 detection."""
    followups = [c for c in author_replies(thread) if _login(c) == CODEX_BOT_LOGIN]
    return followups[-1] if followups else None


def classify_thread(thread: dict) -> ThreadState:
    """Return A/B/C per the SWM-1101 taxonomy. Outdated wins over replied."""
    if thread.get("isOutdated"):
        return "B"
    if latest_author_reply(thread) is not None:
        return "C"
    return "A"

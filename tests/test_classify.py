"""Unit tests for thread state classification."""
from __future__ import annotations

import pytest

from swm import classify


def _thread(*, isOutdated: bool = False, isResolved: bool = False, comments: list[dict] | None = None) -> dict:
    return {
        "id": "T1",
        "isOutdated": isOutdated,
        "isResolved": isResolved,
        "path": "x.py",
        "line": 1,
        "comments": {"nodes": comments or []},
    }


def _comment(login: str, body: str, *, db_id: int = 1, reply_to: int | None = None) -> dict:
    return {
        "databaseId": db_id,
        "author": {"login": login},
        "body": body,
        "createdAt": "2026-05-07T12:00:00Z",
        "replyTo": {"databaseId": reply_to} if reply_to else None,
    }


def test_state_a_when_no_reply_and_not_outdated() -> None:
    # Arrange
    thread = _thread(comments=[_comment("chatgpt-codex-connector", "P2: ...")])

    # Act / Assert
    assert classify.classify_thread(thread) == "A"


def test_state_b_when_outdated() -> None:
    # Arrange — outdated takes precedence even if author also replied
    thread = _thread(isOutdated=True, comments=[
        _comment("chatgpt-codex-connector", "P2: ..."),
        _comment("ryosaeba1985", "fixed in c476c877"),
    ])

    # Act / Assert
    assert classify.classify_thread(thread) == "B"


def test_state_c_when_replied_not_outdated() -> None:
    # Arrange
    thread = _thread(comments=[
        _comment("chatgpt-codex-connector", "P2: ...", db_id=1),
        _comment("ryosaeba1985", "actually no, here's why...", db_id=2, reply_to=1),
    ])

    # Act / Assert
    assert classify.classify_thread(thread) == "C"


def test_is_codex_thread_true_when_first_author_is_codex_bot() -> None:
    # Arrange
    thread = _thread(comments=[_comment("chatgpt-codex-connector", "...")])

    # Act / Assert
    assert classify.is_codex_thread(thread) is True


def test_is_codex_thread_false_when_first_author_is_human() -> None:
    # Arrange
    thread = _thread(comments=[_comment("ryosaeba1985", "manual review")])

    # Act / Assert
    assert classify.is_codex_thread(thread) is False


def test_is_codex_thread_false_when_no_comments() -> None:
    # Act / Assert
    assert classify.is_codex_thread(_thread()) is False


def test_codex_comment_id_returns_first_comment_db_id() -> None:
    # Arrange
    thread = _thread(comments=[_comment("chatgpt-codex-connector", "...", db_id=42)])

    # Act / Assert
    assert classify.codex_comment_id(thread) == 42


def test_latest_author_reply_skips_codex_followups() -> None:
    # Arrange — Codex top, human reply, then a Codex follow-up
    thread = _thread(comments=[
        _comment("chatgpt-codex-connector", "initial"),
        _comment("ryosaeba1985", "fix attempt", db_id=2),
        _comment("chatgpt-codex-connector", "looks good", db_id=3),
    ])

    # Act
    reply = classify.latest_author_reply(thread)

    # Assert
    assert reply is not None
    assert reply["databaseId"] == 2


def test_codex_pr_body_signal_thumbs_up_means_approved() -> None:
    # Arrange — Codex bot left a 👍 reaction on the PR body
    reactions = [{"content": "THUMBS_UP", "user": {"login": "chatgpt-codex-connector[bot]"}, "createdAt": "2026-05-07T12:00:00Z"}]

    # Act / Assert
    assert classify.codex_pr_body_signal(reactions) == "approved"


def test_codex_pr_body_signal_eyes_means_reviewing() -> None:
    # Arrange — Codex bot is still reviewing
    reactions = [{"content": "EYES", "user": {"login": "chatgpt-codex-connector[bot]"}, "createdAt": "2026-05-07T12:00:00Z"}]

    # Act / Assert
    assert classify.codex_pr_body_signal(reactions) == "reviewing"


def test_codex_pr_body_signal_thumbs_up_wins_over_eyes_during_transition() -> None:
    # Arrange — both reactions present (transition window before Codex removes the 👀)
    reactions = [
        {"content": "EYES", "user": {"login": "chatgpt-codex-connector[bot]"}, "createdAt": "2026-05-07T12:00:00Z"},
        {"content": "THUMBS_UP", "user": {"login": "chatgpt-codex-connector[bot]"}, "createdAt": "2026-05-07T12:01:00Z"},
    ]

    # Act / Assert — approved should win
    assert classify.codex_pr_body_signal(reactions) == "approved"


def test_codex_pr_body_signal_ignores_human_reactions() -> None:
    # Arrange — only humans reacted; Codex hasn't engaged
    reactions = [
        {"content": "THUMBS_UP", "user": {"login": "ryosaeba1985"}, "createdAt": "2026-05-07T12:00:00Z"},
        {"content": "HEART", "user": {"login": "frankyxhl"}, "createdAt": "2026-05-07T12:00:00Z"},
    ]

    # Act / Assert
    assert classify.codex_pr_body_signal(reactions) is None


def test_codex_pr_body_signal_handles_empty_list() -> None:
    # Act / Assert
    assert classify.codex_pr_body_signal([]) is None
    assert classify.codex_pr_body_signal(None) is None  # type: ignore[arg-type]


def test_codex_pr_body_signal_accepts_graphql_login_form() -> None:
    # Arrange — GraphQL omits the [bot] suffix; REST includes it. We support both.
    reactions = [{"content": "THUMBS_UP", "user": {"login": "chatgpt-codex-connector"}, "createdAt": "2026-05-07T12:00:00Z"}]

    # Act / Assert
    assert classify.codex_pr_body_signal(reactions) == "approved"


def test_latest_codex_followup_returns_most_recent_bot_reply() -> None:
    # Arrange
    thread = _thread(comments=[
        _comment("chatgpt-codex-connector", "initial"),
        _comment("ryosaeba1985", "fix"),
        _comment("chatgpt-codex-connector", "first followup", db_id=3),
        _comment("chatgpt-codex-connector", "second followup", db_id=4),
    ])

    # Act
    followup = classify.latest_codex_followup(thread)

    # Assert
    assert followup is not None
    assert followup["databaseId"] == 4

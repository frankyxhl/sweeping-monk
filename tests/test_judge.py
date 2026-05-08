"""Unit tests for SWM-1101 verdict assignment + substantive-reply heuristic."""
from __future__ import annotations

import pytest

from swm import judge
from swm.models import Verdict


def test_state_b_with_code_change_is_resolved() -> None:
    # Act
    decision = judge.judge(classification="B", author_reply_body=None, code_changed=True, codex_followup_body=None)

    # Assert
    assert decision.verdict is Verdict.RESOLVED
    assert "outdated" in decision.reason.lower()


def test_state_b_without_code_change_needs_human_judgment() -> None:
    # Arrange — outdated marking but no detected code change is suspicious
    decision = judge.judge(classification="B", author_reply_body=None, code_changed=False, codex_followup_body=None)

    # Assert
    assert decision.verdict is Verdict.NEEDS_HUMAN_JUDGMENT


def test_state_c_with_substantive_reply_is_resolved() -> None:
    # Arrange — reply cites a commit SHA + a filename
    body = (
        "Verified + documented in c476c877. The foot-gun is real but not active today. "
        "Branch protection state on `main` checked via gh api repos/.../branches/main/protection."
    )

    # Act
    decision = judge.judge(classification="C", author_reply_body=body, code_changed=False, codex_followup_body=None)

    # Assert
    assert decision.verdict is Verdict.RESOLVED
    assert decision.substantive is True


def test_state_c_with_short_ack_is_open() -> None:
    # Arrange — bare "thanks" reply
    decision = judge.judge(classification="C", author_reply_body="thanks!", code_changed=False, codex_followup_body=None)

    # Assert
    assert decision.verdict is Verdict.OPEN
    assert decision.substantive is False


def test_state_c_with_long_but_no_identifier_is_open() -> None:
    # Arrange — long enough but generic
    body = "yeah I think you're probably right about this one but I'm not totally sure honestly let me look more later"

    # Act
    decision = judge.judge(classification="C", author_reply_body=body, code_changed=False, codex_followup_body=None)

    # Assert
    assert decision.verdict is Verdict.OPEN


def test_state_a_returns_open_no_response() -> None:
    # Act
    decision = judge.judge(classification="A", author_reply_body=None, code_changed=False, codex_followup_body=None)

    # Assert
    assert decision.verdict is Verdict.OPEN


def test_codex_positive_followup_overrides_to_resolved() -> None:
    # Arrange — even with a non-substantive reply, Codex 👍 wins
    decision = judge.judge(
        classification="C",
        author_reply_body="thanks",
        code_changed=False,
        codex_followup_body="Looks good, no new issues.",
    )

    # Assert
    assert decision.verdict is Verdict.RESOLVED
    assert "follow-up" in decision.reason.lower() or "approval" in decision.reason.lower()


def test_codex_negative_followup_overrides_to_open() -> None:
    # Arrange — even with a substantive reply, Codex 👎 wins
    decision = judge.judge(
        classification="C",
        author_reply_body="Verified in commit c476c877; see file.yml line 31.",
        code_changed=False,
        codex_followup_body="Concern remains: the migration path is still missing.",
    )

    # Assert
    assert decision.verdict is Verdict.OPEN


def test_substantive_reply_helper_directly() -> None:
    # Arrange
    long_with_id = "I checked branches/main/protection via gh api and got 404 — see commit c476c877 for the docs."
    short = "ok"
    long_without_id = "yeah we should probably do something about that maybe later."

    # Act / Assert
    assert judge.is_substantive_reply(long_with_id) is True
    assert judge.is_substantive_reply(short) is False
    assert judge.is_substantive_reply(long_without_id) is False
    assert judge.is_substantive_reply(None) is False


def test_judge_github_isResolved_overrides_all_other_signals():
    """When GitHub itself reports isResolved=true (manual UI resolve, or any
    external sync), trust GitHub as the system of record and return RESOLVED
    regardless of classification / replies / Codex follow-up state.

    Regression: prior behavior left local verdict OPEN even after a manual
    UI resolve, because the classifier never read github_isResolved as a
    verdict input. Surfaced live: sweeping-monk#1 state.py:179 thread."""
    from swm import judge as judge_mod
    decision = judge_mod.judge(
        classification="A",            # nothing else looks resolved
        author_reply_body=None,
        code_changed=False,
        codex_followup_body=None,
        github_isResolved=True,
    )
    assert decision.verdict.value == "RESOLVED"
    assert "GitHub" in decision.reason or "isResolved" in decision.reason


def test_judge_github_isResolved_false_does_not_change_existing_logic(open_thread):
    """github_isResolved=False (the common case) must not perturb step 3-6 logic."""
    from swm import judge as judge_mod
    decision = judge_mod.judge(
        classification="A",
        author_reply_body=None,
        code_changed=False,
        codex_followup_body=None,
        github_isResolved=False,
    )
    # State A with no other signals → OPEN per existing logic
    assert decision.verdict.value == "OPEN"

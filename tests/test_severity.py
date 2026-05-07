"""Unit tests for SWM-1102 severity demotion logic."""
from __future__ import annotations

import pytest

from swm import severity
from swm.models import Severity


def test_required_check_finding_demotes_when_branch_unprotected() -> None:
    # Act
    decision = severity.evaluate(
        codex_severity=Severity.P2,
        finding_kind="required_check_coupling",
        branch_protected=False,
    )

    # Assert
    assert decision.codex_severity is Severity.P2
    assert decision.effective_severity is Severity.P3
    assert decision.reason is not None
    assert "branch protection" in decision.reason.lower()


def test_required_check_finding_keeps_severity_when_branch_protected() -> None:
    # Act
    decision = severity.evaluate(
        codex_severity=Severity.P2,
        finding_kind="required_check_coupling",
        branch_protected=True,
    )

    # Assert
    assert decision.effective_severity is Severity.P2
    assert decision.reason is None


def test_unknown_finding_kind_passes_through_unchanged() -> None:
    # Act
    decision = severity.evaluate(
        codex_severity=Severity.P2,
        finding_kind=None,
        branch_protected=False,
    )

    # Assert
    assert decision.effective_severity is Severity.P2
    assert decision.reason is None


def test_p3_does_not_demote_further() -> None:
    # Arrange — already at the lowest severity
    decision = severity.evaluate(
        codex_severity=Severity.P3,
        finding_kind="required_check_coupling",
        branch_protected=False,
    )

    # Assert
    assert decision.effective_severity is Severity.P3


def test_p1_demotes_one_step_to_p2() -> None:
    # Act
    decision = severity.evaluate(
        codex_severity=Severity.P1,
        finding_kind="required_check_coupling",
        branch_protected=False,
    )

    # Assert
    assert decision.effective_severity is Severity.P2

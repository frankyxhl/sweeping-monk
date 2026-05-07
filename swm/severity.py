"""Severity demotion/promotion per SWM-1102.

Currently implements the highest-value rule: branch-protection-aware demotion
(SWM-1102 §B row 1). The other rows in §B / §C will be added as the watchdog
encounters real PRs that exercise them — this keeps the rule table grounded in
observed evidence rather than speculation.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Severity


@dataclass(frozen=True)
class SeverityDecision:
    """The watchdog's effective severity for a Codex finding, with audit trail."""
    codex_severity: Severity
    effective_severity: Severity
    reason: str | None


def _demote_one_step(sev: Severity) -> Severity:
    if sev is Severity.P1:
        return Severity.P2
    if sev is Severity.P2:
        return Severity.P3
    return sev  # P3 cannot demote further


def evaluate(
    *,
    codex_severity: Severity,
    finding_kind: str | None,
    branch_protected: bool,
) -> SeverityDecision:
    """Apply SWM-1102 §B row 1 demotion when Codex flags a required-check coupling
    and the base branch has no protection rule.

    `finding_kind` is a free-form classifier from the calling code — pass
    "required_check_coupling" to trigger the row-1 demotion. Other values pass
    through unchanged for now (future revisions will add more rows).
    """
    if finding_kind == "required_check_coupling" and not branch_protected and codex_severity is not Severity.P3:
        demoted = _demote_one_step(codex_severity)
        return SeverityDecision(
            codex_severity=codex_severity,
            effective_severity=demoted,
            reason="main has no branch protection (SWM-1102 §B row 1)",
        )
    return SeverityDecision(codex_severity=codex_severity, effective_severity=codex_severity, reason=None)

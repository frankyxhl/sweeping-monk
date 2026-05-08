"""Unit tests for pydantic models — schema validation + round-trip."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from swm.models import (
    CIConclusion,
    PollRecord,
    Severity,
    Status,
    Thread,
    Verdict,
)


def test_poll_record_round_trip(ready_poll: PollRecord) -> None:
    # Arrange
    raw = ready_poll.model_dump_json()

    # Act
    parsed = PollRecord.model_validate_json(raw)

    # Assert
    assert parsed == ready_poll
    assert parsed.status is Status.READY
    assert parsed.threads[0].verdict is Verdict.RESOLVED


def test_state_key_short_circuits_on_identical_polls(pending_poll: PollRecord, ready_poll: PollRecord) -> None:
    # Arrange — make a second poll identical to pending in all key dimensions
    same_as_pending = pending_poll.model_copy(update={"summary": "different text", "trigger": "later-poll"})

    # Act / Assert — only summary/trigger changed → key still equal
    assert pending_poll.state_key() == same_as_pending.state_key()
    # Real change in head/status/codex_open → key differs
    assert pending_poll.state_key() != ready_poll.state_key()


def test_thread_rejects_invalid_severity() -> None:
    # Act / Assert
    with pytest.raises(ValidationError):
        Thread(
            id="t1",
            comment_id=1,
            path="x.py",
            line=1,
            codex_severity="P9",  # type: ignore[arg-type]
            effective_severity=Severity.P3,
            verdict=Verdict.OPEN,
        )


def test_poll_record_rejects_invalid_status() -> None:
    # Act / Assert
    with pytest.raises(ValidationError):
        PollRecord(
            ts="2026-05-07T12:00:00Z",
            repo="o/r",
            pr=1,
            head_sha="a" * 40,
            status="maybe",  # type: ignore[arg-type]
        )


def test_ci_conclusion_round_trip_string_values() -> None:
    # Arrange — CI dict often arrives with raw strings from gh
    rec = PollRecord(
        ts="2026-05-07T12:00:00Z",
        repo="o/r",
        pr=1,
        head_sha="a" * 40,
        status="ready",
        ci={"linux": "SUCCESS", "macos": "FAILURE"},  # type: ignore[arg-type]
    )

    # Assert — strings coerced to enum
    assert rec.ci["linux"] is CIConclusion.SUCCESS
    assert rec.ci["macos"] is CIConclusion.FAILURE


def test_extra_fields_preserved_on_thread() -> None:
    # Arrange — Thread.model_config has extra="allow" so unknown fields survive round-trip
    t = Thread(
        id="t1",
        comment_id=1,
        path="x.py",
        line=1,
        codex_severity=Severity.P3,
        effective_severity=Severity.P3,
        verdict=Verdict.OPEN,
        custom_field="watchdog-vendor-extension",  # type: ignore[call-arg]
    )

    # Act
    parsed = Thread.model_validate_json(t.model_dump_json())

    # Assert
    assert parsed.model_dump().get("custom_field") == "watchdog-vendor-extension"


# --- CHG-1105 BoxMiss model -------------------------------------------------


def test_box_miss_round_trip_serialization() -> None:
    """BoxMiss must JSON-round-trip through pydantic; this is the on-disk shape."""
    from datetime import datetime, timezone
    from swm.models import BoxMiss
    miss = BoxMiss(
        ts=datetime(2026, 5, 8, 1, 53, 29, tzinfo=timezone.utc),
        repo="frankyxhl/trinity",
        pr=67,
        head_sha="5c53bd43289d8ca8297fffb0e93b7d42aa6892a7",
        box_text="CI ubuntu-latest passes",
        rule_id="ci.ubuntu",
        satisfied=False,
        reason="no CI runs (paths-ignore / docs-only); parent verdict=pending",
    )
    raw = miss.model_dump_json()
    loaded = BoxMiss.model_validate_json(raw)
    assert loaded == miss


def test_box_miss_accepts_null_rule_id_for_coverage_gap_branch() -> None:
    """When no BOX_RULES regex matched, rule_id is None and satisfied stays False.
    This is the coverage-gap branch — distinct from predicate-refused."""
    from datetime import datetime, timezone
    from swm.models import BoxMiss
    miss = BoxMiss(
        ts=datetime(2026, 5, 8, 1, 0, 0, tzinfo=timezone.utc),
        repo="x/y",
        pr=1,
        head_sha="abc12345",
        box_text="CHANGELOG updated",
        reason="no rule matched — manual check required",
    )
    assert miss.rule_id is None
    assert miss.satisfied is False

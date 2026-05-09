"""CHG-1112 RED-phase tests for the positive-transition notifier.

Targets (none yet implemented — this file is intentionally failing):
  * `swm.notify.detect_positive_transition`
  * `swm.notify.NotificationRecord`
  * `swm.notify.format_suggested_action`
  * `swm.state.StateStore.append_notification`

Test names mirror the SWM-1112 §Risks table so each named risk has a
named test for traceability.
"""
from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from swm.models import CIConclusion, PollRecord, Status
from swm.state import StateStore
from tests.conftest import FakeGhClient


def _ts(minute: int = 0) -> datetime:
    return datetime(2026, 5, 9, 12, minute, 0, tzinfo=timezone.utc)


_UNSET: Any = object()  # sentinel — distinguishes "default ci" from "explicit {}"


def _record(
    *,
    status: Status,
    head_sha: str = "a" * 40,
    repo: str = "owner/repo",
    pr: int = 49,
    title: str | None = "ci: skip docs",
    codex_open: int = 0,
    ci: dict[str, CIConclusion] | Any = _UNSET,
    ts_minute: int = 0,
) -> PollRecord:
    """Tiny PollRecord factory — only the dimensions the detector cares about.
    Pass `ci={}` to model an empty-CI (paths-ignore / docs-only) PR; the
    sentinel preserves the distinction between "use default" and "explicit
    empty" that `or` would collapse."""
    return PollRecord(
        ts=_ts(ts_minute),
        repo=repo,
        pr=pr,
        title=title,
        head_sha=head_sha,
        status=status,
        ci=({"ci": CIConclusion.SUCCESS} if ci is _UNSET else ci),
        merge_state="CLEAN",
        codex_open=codex_open,
        codex_resolved=0,
        threads=[],
        trigger="poll-cycle",
    )


# --- Detector branch tests (named per SWM-1112 §Risks) ---------------------


def test_no_notification_when_state_key_unchanged() -> None:
    """Risk row 1: detector must return None when state_key is identical.

    The CHG-1107 short-circuit guards this in production, but the detector
    itself must also be idempotent — same head, same status, same CI ⇒ no
    notification, even if invoked directly.
    """
    from swm.notify import detect_positive_transition

    prior = _record(status=Status.READY, head_sha="a" * 40)
    new = _record(status=Status.READY, head_sha="a" * 40, ts_minute=5)

    assert detect_positive_transition(prior, new) is None


def test_no_notification_when_ready_polled_repeatedly_with_same_head() -> None:
    """Risk row 1 (companion): repeated READY polls at the same head_sha
    must never re-emit `ready-after-head-bump`."""
    from swm.notify import detect_positive_transition

    prior = _record(status=Status.READY, head_sha="b" * 40)
    new = _record(status=Status.READY, head_sha="b" * 40, ts_minute=5)

    assert detect_positive_transition(prior, new) is None


def test_first_observation_of_ready_emits_first_ready() -> None:
    """Risk row 2: first poll for a PR (prior is None) that lands READY
    must emit `first-ready`."""
    from swm.notify import detect_positive_transition

    new = _record(status=Status.READY, head_sha="c" * 40)

    assert detect_positive_transition(None, new) == "first-ready"


def test_pending_to_ready_emits_pending_to_ready() -> None:
    from swm.notify import detect_positive_transition

    prior = _record(status=Status.PENDING, head_sha="d" * 40)
    new = _record(status=Status.READY, head_sha="d" * 40, ts_minute=5)

    assert detect_positive_transition(prior, new) == "pending-to-ready"


def test_blocked_to_ready_emits_blocked_to_ready() -> None:
    from swm.notify import detect_positive_transition

    prior = _record(status=Status.BLOCKED, head_sha="e" * 40)
    new = _record(status=Status.READY, head_sha="e" * 40, ts_minute=5)

    assert detect_positive_transition(prior, new) == "blocked-to-ready"


def test_ready_to_ready_at_new_head_emits_ready_after_head_bump() -> None:
    """READY → READY but head_sha differs: re-notify on new head."""
    from swm.notify import detect_positive_transition

    prior = _record(status=Status.READY, head_sha="f" * 40)
    new = _record(status=Status.READY, head_sha="9" * 40, ts_minute=5)

    assert detect_positive_transition(prior, new) == "ready-after-head-bump"


def test_blocked_to_ready_at_new_head_emits_blocked_to_ready() -> None:
    """Risk row 3 — branch order discriminator. When prior=BLOCKED and
    new=READY AND head differs, the transition must be `blocked-to-ready`,
    NOT `ready-after-head-bump`. The blocked-to-ready branch must come
    BEFORE the head-bump branch in the detector, otherwise BLOCKED→READY
    rebounds at a new head get mis-labeled as routine head bumps.
    """
    from swm.notify import detect_positive_transition

    prior = _record(status=Status.BLOCKED, head_sha="1" * 40)
    new = _record(status=Status.READY, head_sha="2" * 40, ts_minute=5)

    transition = detect_positive_transition(prior, new)
    assert transition == "blocked-to-ready", (
        f"branch-order bug: BLOCKED→READY at new head must be 'blocked-to-ready', "
        f"not '{transition}'. The status-change discriminator outranks head-bump."
    )


def test_reason_does_not_claim_ci_green_when_ci_is_empty() -> None:
    """Codex PR #11 review #3212833878: `_compute_status` returns READY for
    paths-ignore / absent-CI PRs (empty `statusCheckRollup` after the grace
    period). Hardcoding `reason = '... ci green, no blocking findings'`
    puts a false claim into the approval audit trail. The reason wording
    must be derived from `new.ci`: empty → 'no required CI configured';
    all SUCCESS → 'ci green (N checks)'.
    """
    from swm.notify import NotificationRecord

    new = _record(status=Status.READY, head_sha="6" * 40, ci={})  # docs-only PR
    note = NotificationRecord.from_transition(None, new, "first-ready")

    assert "ci green" not in note.suggested_action, (
        f"reason falsely claims ci green when ci is empty. "
        f"action={note.suggested_action!r}"
    )
    assert "no required CI configured" in note.suggested_action, (
        f"reason should name the empty-CI case explicitly. "
        f"action={note.suggested_action!r}"
    )


def test_reason_names_check_count_when_ci_green() -> None:
    """When `new.ci` has at least one check and all SUCCESS, the reason
    must say `ci green (N checks)` so the approval evidence is honest."""
    from swm.notify import NotificationRecord

    new = _record(
        status=Status.READY, head_sha="7" * 40,
        ci={"build": CIConclusion.SUCCESS, "test": CIConclusion.SUCCESS},
    )
    note = NotificationRecord.from_transition(None, new, "first-ready")

    assert "ci green (2 checks)" in note.suggested_action, (
        f"action did not include the check count. action={note.suggested_action!r}"
    )


@pytest.mark.parametrize("non_ready_prior_status", [Status.ERROR, Status.SKIPPED])
def test_head_bump_branch_does_not_misfire_on_error_or_skipped_prior(
    non_ready_prior_status: Status,
) -> None:
    """Codex PR #11 review #3212804778: when `prior.status` is ERROR or SKIPPED
    and `new.status == READY` at a different head, the head-bump branch must
    NOT return `ready-after-head-bump` — that semantic is for READY→READY only.
    The detector returns None and the maintainer learns nothing (per CHG-1112
    §Out-of-scope: `recovered-to-ready` is deferred to its own CHG)."""
    from swm.notify import detect_positive_transition

    prior = _record(status=non_ready_prior_status, head_sha="4" * 40)
    new = _record(status=Status.READY, head_sha="5" * 40, ts_minute=5)

    assert detect_positive_transition(prior, new) is None


@pytest.mark.parametrize(
    "non_ready_status",
    [Status.PENDING, Status.BLOCKED, Status.ERROR, Status.SKIPPED],
)
def test_no_notification_when_new_status_is_not_ready(non_ready_status: Status) -> None:
    """The detector only fires on positive (→READY) transitions. Anything
    else — pending, blocked, error, skipped — yields no notification."""
    from swm.notify import detect_positive_transition

    prior = _record(status=Status.READY, head_sha="3" * 40)
    new = _record(status=non_ready_status, head_sha="3" * 40, ts_minute=5)

    assert detect_positive_transition(prior, new) is None


# --- Suggested-action shell-safety -----------------------------------------


def test_suggested_action_quotes_unsafe_reason() -> None:
    """Risk row 5: `suggested_action` must `shlex.quote()` the reason
    payload. PR titles must NEVER appear in `suggested_action` (they live
    in `summary`)."""
    import shlex

    from swm.notify import format_suggested_action

    nasty_reason = "CI green; codex says \"approved\" && rm -rf /"
    nasty_title = "feat: pwn'd $(rm -rf $HOME)"

    action = format_suggested_action(
        repo="frankyxhl/swm",
        pr=9,
        reason=nasty_reason,
        title=nasty_title,  # signature MUST accept title; impl must NOT embed it
    )

    # The reason must round-trip through shlex.quote() — i.e. the quoted form
    # appears literally in the suggested_action string.
    quoted = shlex.quote(nasty_reason)
    assert quoted in action, (
        f"reason was not shlex.quote()'d: action={action!r}, expected {quoted!r}"
    )

    # The PR title must NOT appear anywhere in the suggested action — neither
    # raw nor quoted. summary carries the title separately.
    assert nasty_title not in action
    assert shlex.quote(nasty_title) not in action

    # Sanity: it should look like the documented `swm approve` call.
    assert "swm approve frankyxhl/swm 9" in action
    assert "--reason" in action


# --- Static architectural invariant: detector ↔ state_key coupling --------


def test_detector_only_branches_on_state_key_dimensions() -> None:
    """Risk row 6 — locks the round-1 invariant in CI.

    Every dimension `detect_positive_transition` branches on MUST also be a
    member of `PollRecord.state_key()`. Otherwise the CHG-1107 short-circuit
    fires first and the new branch is dead code (the bug that killed the
    round-1 `codex-approved-on-ready` branch).

    This is a static check on the function source — we read it via
    `inspect.getsource` and verify it only references attribute names that
    belong to the state_key tuple (`status`, `head_sha`), plus the
    whitelisted `prior is None` constant.
    """
    from swm import notify

    src = inspect.getsource(notify.detect_positive_transition)

    # Whitelisted attribute names — every state_key dimension that's a plain
    # attribute on PollRecord. (`pr`, `codex_open`, `ci` are state_key
    # members too but are not currently used in any defined transition;
    # adding them later is fine — they're already in state_key.)
    whitelist = {
        "status",     # state_key dim
        "head_sha",   # state_key dim
        "pr",         # state_key dim (not currently branched on; allowed)
        "codex_open", # state_key dim (not currently branched on; allowed)
        "ci",         # state_key dim (not currently branched on; allowed)
    }

    # Find every `prior.<attr>` and `new.<attr>` (and `record.<attr>`) access.
    attr_pattern = re.compile(r"\b(?:prior|new|record)\.([A-Za-z_][A-Za-z_0-9]*)")
    used = {m.group(1) for m in attr_pattern.finditer(src)}

    # Strip method calls — `state_key()` etc. are fine (they collapse to
    # the whole tuple, not a forbidden new dimension).
    method_pattern = re.compile(r"\b(?:prior|new|record)\.([A-Za-z_][A-Za-z_0-9]*)\s*\(")
    methods = {m.group(1) for m in method_pattern.finditer(src)}
    used -= methods

    forbidden = used - whitelist
    assert not forbidden, (
        f"detector branches on attribute(s) {sorted(forbidden)} that are NOT "
        f"members of PollRecord.state_key(). The CHG-1107 short-circuit will "
        f"render those branches unreachable. Either drop the branch or add "
        f"the dimension to state_key in the same PR. "
        f"Whitelist (state_key members): {sorted(whitelist)}"
    )

    # Belt-and-braces: the function must explicitly handle `prior is None`.
    assert re.search(r"\bprior\s+is\s+None\b", src), (
        "detector must explicitly branch on `prior is None` for first-ready"
    )


# --- StateStore.append_notification round-trip + crash-safety --------------


def test_append_notification_writes_to_notifications_log(
    store: StateStore,
) -> None:
    """`StateStore.append_notification` must write a JSONL line to the
    already-declared `notifications_log` path (declared at swm/state.py:51).
    """
    from swm.notify import NotificationRecord

    note = NotificationRecord(
        ts=_ts(10),
        repo="owner/repo",
        pr=49,
        title="ci: skip docs",
        head_sha="a" * 40,
        transition="first-ready",
        suggested_action="swm approve owner/repo 49 --reason 'first ready'",
        summary="first observation: ci green, no codex threads",
    )

    store.append_notification(note)

    assert store.notifications_log.exists()
    lines = store.notifications_log.read_text().strip().splitlines()
    assert len(lines) == 1
    round_tripped = NotificationRecord.model_validate_json(lines[0])
    assert round_tripped.transition == "first-ready"
    assert round_tripped.pr == 49


def test_notification_write_failure_does_not_drop_poll_record(
    store: StateStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Risk row 7: if `append_notification` raises (disk full, permission
    error), the underlying `append_poll` call must already have happened.
    The poll audit trail is unbroken — only the notification side-channel
    is lost.

    Wiring invariant from SWM-1112 §What snippet:
        store.append_poll(new_record)             # line 1 — MUST happen
        transition = detect_positive_transition(...)
        if transition is not None:
            store.append_notification(note)       # line N — may fail
    """
    from swm.poll import poll

    # Seed a prior PENDING poll so the next poll triggers a pending→ready
    # transition (which exercises the notification branch).
    prior = _record(status=Status.PENDING, head_sha="9" * 40)
    store.append_poll(prior)

    # Capture every append_poll call so we can assert it ran before the
    # IOError.
    real_append_poll = StateStore.append_poll
    poll_calls: list[PollRecord] = []

    def tracking_append_poll(self: StateStore, record: PollRecord) -> None:
        poll_calls.append(record)
        real_append_poll(self, record)

    monkeypatch.setattr(StateStore, "append_poll", tracking_append_poll)

    # Make append_notification blow up — and track whether it was invoked.
    notify_calls: list[Any] = []

    def boom(self: StateStore, note: Any) -> None:
        notify_calls.append(note)
        raise IOError("simulated disk full")

    monkeypatch.setattr(StateStore, "append_notification", boom, raising=False)

    # Build a gh fixture that returns a single READY PR at a NEW head_sha
    # → pending→ready transition → detector fires → append_notification raises.
    fake = FakeGhClient(prs=[{
        "number": 49,
        "title": "ci: skip docs",
        "headRefOid": "f" * 40,  # different head from prior
        "baseRefName": "main",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}],
        "updatedAt": "2026-05-09T12:00:00Z",
    }])

    # The poll() call may either propagate the IOError (acceptable — the
    # CHG spec says "surfaces the failure on stderr") or swallow it; the
    # invariant we lock here is that append_poll for the NEW record
    # happened FIRST regardless.
    try:
        poll("owner/repo", store=store, gh_client=fake)
    except IOError:
        pass  # acceptable per spec ("surfaces the failure")

    # The detector branch must have been reached — append_notification was
    # called (and raised). If this fails, the wiring forgot to invoke the
    # notifier on a real pending→ready transition.
    assert notify_calls, (
        "append_notification was never called: poll() did not invoke the "
        "CHG-1112 detector on a pending→ready transition (production wiring "
        "missing — RED phase will fail here until GREEN lands)."
    )

    # The new poll record must have been appended BEFORE the notification
    # crash. Order matters — the audit trail is unbroken even when the
    # notification side-channel fails.
    new_records = [r for r in poll_calls if r.head_sha == "f" * 40]
    assert new_records, (
        "append_poll(new_record) MUST be called before append_notification "
        "so a notification-write failure cannot drop the poll audit row. "
        f"Saw append_poll calls for heads: {[r.head_sha[:8] for r in poll_calls]}"
    )

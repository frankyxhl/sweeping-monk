"""Unit tests for swm.guarded — SWM-1103 gate logic."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swm import guarded
from swm.guarded import (
    BoxClassification,
    CheckboxLine,
    apply_box_flips,
    build_approve_ledger_entry,
    build_tick_ledger_entry,
    check_identity,
    check_verdict,
    classify_box,
    parse_unchecked_boxes,
    render_approve_body,
)
from swm.models import CIConclusion, LedgerAction, PollRecord, Status

from tests.conftest import FakeGhClient


# --- identity ---------------------------------------------------------------


def test_check_identity_self_action_blocked():
    """When the active gh account authored the PR, GitHub will block self-approval."""
    gh = FakeGhClient(prs=[{"number": 66, "author": {"login": "ryosaeba1985"}}], active_login="ryosaeba1985")
    identity = check_identity(gh, "frankyxhl/trinity", 66)
    assert identity.is_self_action is True
    assert identity.can_proceed is False
    assert "self-approval" in identity.blocker


def test_check_identity_preferred_account_proceeds():
    gh = FakeGhClient(prs=[{"number": 66, "author": {"login": "someone-else"}}], active_login="ryosaeba1985")
    identity = check_identity(gh, "frankyxhl/trinity", 66)
    assert identity.is_self_action is False
    assert identity.is_preferred_identity is True
    assert identity.can_proceed is True
    assert identity.blocker is None


def test_check_identity_non_preferred_proceeds_with_no_blocker():
    """frankyxhl approving a ryosaeba1985-authored PR — allowed, just not preferred."""
    gh = FakeGhClient(prs=[{"number": 66, "author": {"login": "ryosaeba1985"}}], active_login="frankyxhl")
    identity = check_identity(gh, "frankyxhl/trinity", 66)
    assert identity.is_self_action is False
    assert identity.is_preferred_identity is False
    assert identity.can_proceed is True
    assert identity.blocker is None


# --- verdict / freshness ----------------------------------------------------


def test_check_verdict_no_poll(store):
    v = check_verdict(store, "owner/repo", 49, "abc12345")
    assert v.has_poll is False
    ok, why = v.supports_approve()
    assert ok is False and "no recorded poll" in why


def test_check_verdict_status_not_ready(store, pending_poll: PollRecord):
    store.append_poll(pending_poll)
    v = check_verdict(store, pending_poll.repo, pending_poll.pr, pending_poll.head_sha)
    assert v.has_poll is True
    ok, why = v.supports_approve()
    assert ok is False and "not ready" in why


def test_check_verdict_stale_head_sha(store, ready_poll: PollRecord):
    store.append_poll(ready_poll)
    v = check_verdict(store, ready_poll.repo, ready_poll.pr, "newhead123" + "0" * 30)
    assert v.head_sha_fresh is False
    ok, why = v.supports_approve()
    assert ok is False and "re-poll first" in why


def test_check_verdict_supports_approve_when_ready_and_fresh(store, ready_poll: PollRecord):
    store.append_poll(ready_poll)
    v = check_verdict(store, ready_poll.repo, ready_poll.pr, ready_poll.head_sha)
    ok, why = v.supports_approve()
    assert ok is True and why is None


def test_check_verdict_supports_tick_does_not_require_ready(store, pending_poll: PollRecord):
    """tick is just a body edit — it only needs head freshness, not status=ready."""
    store.append_poll(pending_poll)
    v = check_verdict(store, pending_poll.repo, pending_poll.pr, pending_poll.head_sha)
    ok, why = v.supports_tick()
    assert ok is True


# --- approve body ------------------------------------------------------------


def test_render_approve_body_includes_factual_signals(ready_poll: PollRecord):
    body = render_approve_body(ready_poll, "maintainer ok'd one-shot")
    assert ready_poll.head_sha[:8] in body
    assert "ubuntu-latest" in body and "macos-latest" in body
    assert "SUCCESS" in body
    assert "maintainer ok'd one-shot" in body


# --- checkbox parsing --------------------------------------------------------


SAMPLE_BODY = """## Acceptance

- [x] **A1**: parser written
- [ ] **A12**: CI ubuntu-latest + macos-latest
- [ ] CI ubuntu-latest passes (`make test` clean)
- [ ] CI macos-latest passes
- [ ] Codex GitHub bot review
- [ ] Manual smoke on staging
"""


def test_parse_unchecked_boxes_skips_checked_lines():
    boxes = parse_unchecked_boxes(SAMPLE_BODY)
    assert [b.line_number for b in boxes] == [4, 5, 6, 7, 8]
    assert not any("parser written" in b.text for b in boxes)  # the [x] line is excluded


def test_parse_unchecked_boxes_handles_indented_lines():
    body = "  - [ ] indented\n- [ ] flush\n"
    boxes = parse_unchecked_boxes(body)
    assert len(boxes) == 2
    assert boxes[0].text == "indented"


def test_classify_box_ci_ubuntu_satisfied(ready_poll):
    boxes = parse_unchecked_boxes(SAMPLE_BODY)
    ubuntu_box = next(b for b in boxes if b.text.startswith("CI ubuntu"))
    c = classify_box(ubuntu_box, ready_poll)
    assert c.rule_id == "ci.ubuntu"
    assert c.satisfied is True
    assert c.should_flip is True


def test_classify_box_ci_ubuntu_not_satisfied(pending_poll):
    """pending_poll has macos IN_PROGRESS — ubuntu rule should still pass since ubuntu=SUCCESS."""
    boxes = parse_unchecked_boxes(SAMPLE_BODY)
    ubuntu_box = next(b for b in boxes if b.text.startswith("CI ubuntu-latest passes"))
    c = classify_box(ubuntu_box, pending_poll)
    assert c.rule_id == "ci.ubuntu"
    assert c.satisfied is True


def test_classify_box_ci_both_unsatisfied_when_one_runner_in_progress(pending_poll):
    boxes = parse_unchecked_boxes(SAMPLE_BODY)
    a12_box = next(b for b in boxes if "A12" in b.text)
    c = classify_box(a12_box, pending_poll)
    assert c.rule_id == "ci.both"
    assert c.satisfied is False


def test_classify_box_codex_review(ready_poll):
    # ready_poll fixture doesn't set codex_pr_body_signal — patch it for this test.
    poll = ready_poll.model_copy(update={"codex_pr_body_signal": "approved"})
    boxes = parse_unchecked_boxes(SAMPLE_BODY)
    codex_box = next(b for b in boxes if "Codex" in b.text)
    c = classify_box(codex_box, poll)
    assert c.rule_id == "codex.review"
    assert c.satisfied is True


def test_classify_box_manual_smoke_unverifiable(ready_poll):
    boxes = parse_unchecked_boxes(SAMPLE_BODY)
    smoke_box = next(b for b in boxes if "Manual smoke" in b.text)
    c = classify_box(smoke_box, ready_poll)
    assert c.rule_id is None
    assert c.should_flip is False
    assert "manual" in c.reason.lower()


def test_apply_box_flips_only_targets_listed_lines():
    new = apply_box_flips(SAMPLE_BODY, [5, 6])
    assert new.count("- [x]") == 3  # 1 pre-existing + 2 we flipped
    assert new.count("- [ ]") == 3  # 5 - 2 flipped
    # A12 (line 4) and Codex (line 7) and smoke (line 8) stay unchecked
    lines = new.splitlines()
    assert "- [ ] **A12**" in lines[3]
    assert "- [ ] Codex" in lines[6]


def test_apply_box_flips_idempotent_when_targets_already_checked():
    body = "- [x] already checked\n- [ ] flip me\n"
    out = apply_box_flips(body, [1, 2])
    # Line 1 is already checked — apply_box_flips skips lines whose UNCHECKED_RE doesn't match.
    assert out.startswith("- [x] already checked\n")
    assert "- [x] flip me" in out


def test_apply_box_flips_preserves_trailing_lines_without_newline():
    body = "- [ ] one\n- [ ] two"  # no trailing newline on last line
    out = apply_box_flips(body, [2])
    assert out == "- [ ] one\n- [x] two"


# --- ledger entry construction ---------------------------------------------


def test_build_approve_ledger_entry_records_action_and_evidence(ready_poll):
    entry = build_approve_ledger_entry(
        poll=ready_poll, actor="frankyxhl", reason="r",
        authorized_by="maintainer", review_result={"reviewDecision": "APPROVED"},
    )
    assert entry.action == LedgerAction.SUBMIT_REVIEW_APPROVE
    assert entry.head_sha == ready_poll.head_sha
    assert entry.evidence["verdict"] == "ready"
    assert entry.evidence["ci"] == {"ubuntu-latest": "SUCCESS", "macos-latest": "SUCCESS"}
    assert entry.result == {"reviewDecision": "APPROVED"}


def test_build_tick_ledger_entry_records_each_box(ready_poll):
    flipped = [
        BoxClassification(
            box=CheckboxLine(raw="- [ ] CI ubuntu-latest passes", text="CI ubuntu-latest passes", line_number=5),
            rule_id="ci.ubuntu",
            satisfied=True,
            reason="ubuntu-latest = SUCCESS",
        ),
    ]
    entry = build_tick_ledger_entry(
        poll=ready_poll, actor="frankyxhl",
        authorized_by="maintainer", reason="auto-tick", flipped=flipped,
    )
    assert entry.action == LedgerAction.EDIT_PR_BODY_CHECK_BOXES
    assert entry.evidence["boxes_flipped"][0]["rule"] == "ci.ubuntu"
    assert entry.result["diff_lines_changed"] == 1


# --- docs-only / paths-ignore CI handling (round-2 fix) ---------------------


def test_classify_box_ci_runner_empty_ci_with_ready_status_is_satisfied(ready_poll):
    """When CI dict is empty AND parent verdict already trusts it (status=ready),
    treat 'CI ubuntu-latest passes' as satisfied — paths-ignore / docs-only case."""
    poll = ready_poll.model_copy(update={"ci": {}})
    boxes = parse_unchecked_boxes(SAMPLE_BODY)
    ubuntu_box = next(b for b in boxes if b.text.startswith("CI ubuntu-latest passes"))
    c = classify_box(ubuntu_box, poll)
    assert c.rule_id == "ci.ubuntu"
    assert c.satisfied is True
    assert "paths-ignore" in c.reason


def test_classify_box_ci_runner_empty_ci_with_pending_status_not_satisfied(pending_poll):
    """When CI dict is empty BUT parent verdict isn't yet ready (still in grace
    window, or transient), do NOT trust the empty-CI state."""
    poll = pending_poll.model_copy(update={"ci": {}})
    boxes = parse_unchecked_boxes(SAMPLE_BODY)
    ubuntu_box = next(b for b in boxes if b.text.startswith("CI ubuntu-latest passes"))
    c = classify_box(ubuntu_box, poll)
    assert c.rule_id == "ci.ubuntu"
    assert c.satisfied is False
    assert "not yet trusted" in c.reason


def test_classify_box_ci_both_empty_ci_with_ready_status_is_satisfied(ready_poll):
    """Same trust transfer applies to the 'CI ubuntu+macos' combined rule."""
    poll = ready_poll.model_copy(update={"ci": {}})
    boxes = parse_unchecked_boxes(SAMPLE_BODY)
    a12_box = next(b for b in boxes if "A12" in b.text)
    c = classify_box(a12_box, poll)
    assert c.rule_id == "ci.both"
    assert c.satisfied is True


# --- CHG-1105 build_box_miss ------------------------------------------------


def test_build_box_miss_records_unmatched_rule_branch(ready_poll):
    """When rule_id is None (no BOX_RULES regex matched), box_miss carries that
    truthfully so the rule-coverage report can flag a coverage gap."""
    from swm.guarded import build_box_miss, BoxClassification, CheckboxLine
    classification = BoxClassification(
        box=CheckboxLine(raw="- [ ] CHANGELOG updated", text="CHANGELOG updated", line_number=12),
        rule_id=None,
        satisfied=False,
        reason="no rule matched — manual check required",
    )
    miss = build_box_miss(classification=classification, poll=ready_poll)
    assert miss.repo == ready_poll.repo
    assert miss.pr == ready_poll.pr
    assert miss.head_sha == ready_poll.head_sha
    assert miss.box_text == "CHANGELOG updated"
    assert miss.rule_id is None
    assert "no rule matched" in miss.reason


def test_build_box_miss_records_predicate_refused_branch(pending_poll):
    """When a rule matched but its predicate said not satisfied, both rule_id
    and the refusal reason are preserved — enables count-by-rule analysis."""
    from swm.guarded import build_box_miss, BoxClassification, CheckboxLine
    poll = pending_poll.model_copy(update={"ci": {}})
    classification = BoxClassification(
        box=CheckboxLine(raw="- [ ] CI ubuntu-latest passes", text="CI ubuntu-latest passes", line_number=5),
        rule_id="ci.ubuntu",
        satisfied=False,
        reason="no CI runs and parent verdict=pending (not yet trusted)",
    )
    miss = build_box_miss(classification=classification, poll=poll)
    assert miss.rule_id == "ci.ubuntu"
    assert "not yet trusted" in miss.reason


def test_build_box_miss_uses_now_utc_when_ts_omitted(ready_poll):
    """Default ts must be a tz-aware UTC datetime — so since-window filters work."""
    from swm.guarded import build_box_miss, BoxClassification, CheckboxLine
    classification = BoxClassification(
        box=CheckboxLine(raw="- [ ] x", text="x", line_number=1),
        rule_id=None, satisfied=False, reason="r",
    )
    miss = build_box_miss(classification=classification, poll=ready_poll)
    assert miss.ts.tzinfo is not None  # tz-aware

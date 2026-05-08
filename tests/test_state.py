"""Unit tests for StateStore — JSONL append + read, thread snapshots."""
from __future__ import annotations

from datetime import datetime, timezone

from swm.models import LedgerAction, LedgerEntry, PollRecord, ThreadSnapshot, Verdict
from swm.state import StateStore


def test_append_and_read_polls_round_trip(store: StateStore, pending_poll: PollRecord) -> None:
    # Act
    store.append_poll(pending_poll)
    records = list(store.read_polls())

    # Assert
    assert len(records) == 1
    assert records[0] == pending_poll


def test_read_polls_filters_by_repo(store: StateStore, pending_poll: PollRecord) -> None:
    # Arrange — write polls for two different repos
    other = pending_poll.model_copy(update={"repo": "other/repo"})
    store.append_poll(pending_poll)
    store.append_poll(other)

    # Act
    primary = list(store.read_polls(repo=pending_poll.repo))
    secondary = list(store.read_polls(repo="other/repo"))

    # Assert
    assert {r.repo for r in primary} == {pending_poll.repo}
    assert {r.repo for r in secondary} == {"other/repo"}


def test_read_polls_filters_by_pr_number(store: StateStore, pending_poll: PollRecord, ready_poll: PollRecord) -> None:
    # Arrange — same repo, different PR numbers
    other_pr = pending_poll.model_copy(update={"pr": 50})
    store.append_poll(pending_poll)
    store.append_poll(other_pr)
    store.append_poll(ready_poll)

    # Act
    pr49 = list(store.read_polls(pr=49))
    pr50 = list(store.read_polls(pr=50))

    # Assert
    assert {r.pr for r in pr49} == {49}
    assert {r.pr for r in pr50} == {50}


def test_latest_poll_returns_most_recent(store: StateStore, pending_poll: PollRecord, ready_poll: PollRecord) -> None:
    # Arrange — append in chronological order
    store.append_poll(pending_poll)
    store.append_poll(ready_poll)

    # Act
    latest = store.latest_poll(repo=pending_poll.repo, pr=pending_poll.pr)

    # Assert — last appended wins (since we order by file order, not ts)
    assert latest == ready_poll


def test_latest_poll_returns_none_for_unknown_pr(store: StateStore, pending_poll: PollRecord) -> None:
    # Arrange
    store.append_poll(pending_poll)

    # Act
    missing = store.latest_poll(repo=pending_poll.repo, pr=9999)

    # Assert
    assert missing is None


def test_latest_per_pr_indexes_by_pr_number(store: StateStore, pending_poll: PollRecord, ready_poll: PollRecord) -> None:
    # Arrange — two polls for same PR, one for different PR
    other = pending_poll.model_copy(update={"pr": 50})
    store.append_poll(pending_poll)
    store.append_poll(ready_poll)
    store.append_poll(other)

    # Act
    by_pr = store.latest_per_pr(pending_poll.repo)

    # Assert
    assert set(by_pr.keys()) == {49, 50}
    assert by_pr[49] == ready_poll  # most recent for pr 49
    assert by_pr[50] == other


def test_thread_snapshot_round_trip(store: StateStore, thread_snapshot: ThreadSnapshot) -> None:
    # Act
    store.write_thread(thread_snapshot)
    loaded = store.read_thread(thread_snapshot.repo, thread_snapshot.pr, thread_snapshot.thread_id)

    # Assert
    assert loaded == thread_snapshot
    assert loaded is not None
    assert loaded.verdict is Verdict.RESOLVED
    assert loaded.evidence.code_change_commit == "abc12345"


def test_read_thread_returns_none_when_missing(store: StateStore) -> None:
    # Act / Assert
    assert store.read_thread("owner/repo", 99, "nonexistent_thread_id") is None
    assert store.read_thread_history("owner/repo", 99, "nonexistent_thread_id") == []


def test_write_thread_appends_history_never_overwrites(store: StateStore, thread_snapshot: ThreadSnapshot) -> None:
    # Arrange — write three snapshots representing successive polls
    snap_v1 = thread_snapshot
    snap_v2 = thread_snapshot.model_copy(update={"verdict": Verdict.NEEDS_HUMAN_JUDGMENT})
    snap_v3 = thread_snapshot.model_copy(update={"verdict": Verdict.RESOLVED})
    store.write_thread(snap_v1)
    store.write_thread(snap_v2)
    store.write_thread(snap_v3)

    # Act
    history = store.read_thread_history(snap_v1.repo, snap_v1.pr, snap_v1.thread_id)
    latest = store.read_thread(snap_v1.repo, snap_v1.pr, snap_v1.thread_id)

    # Assert — full chronological history retained, latest = last write
    assert [s.verdict for s in history] == [snap_v1.verdict, snap_v2.verdict, snap_v3.verdict]
    assert latest is not None
    assert latest.verdict is Verdict.RESOLVED


def test_threads_are_isolated_per_pr(store: StateStore, thread_snapshot: ThreadSnapshot) -> None:
    """The same thread_id collides across PRs only if we mishandle paths.
    GraphQL node IDs are unique in practice but the path layout must scope by PR."""
    # Arrange — two snapshots for the same thread_id but different PR numbers
    snap_pr_49 = thread_snapshot
    snap_pr_50 = thread_snapshot.model_copy(update={"pr": 50, "verdict": Verdict.OPEN})
    store.write_thread(snap_pr_49)
    store.write_thread(snap_pr_50)

    # Act
    pr49 = store.read_thread(snap_pr_49.repo, 49, thread_snapshot.thread_id)
    pr50 = store.read_thread(snap_pr_50.repo, 50, thread_snapshot.thread_id)

    # Assert — each PR's history is independent
    assert pr49 is not None and pr49.verdict is Verdict.RESOLVED
    assert pr50 is not None and pr50.verdict is Verdict.OPEN


def test_pr_directory_layout_groups_polls_and_threads(store: StateStore, ready_poll, thread_snapshot) -> None:
    """A PR's polls.jsonl + threads/ live in the same directory — easy to gc."""
    # Arrange + Act
    store.append_poll(ready_poll)
    store.write_thread(thread_snapshot)

    # Assert
    pr_dir = store.directory / "owner" / "repo" / "pr-49"
    assert (pr_dir / "polls.jsonl").exists()
    assert (pr_dir / "threads" / f"{thread_snapshot.thread_id}.jsonl").exists()


def test_read_polls_handles_missing_log(store: StateStore) -> None:
    # Act — directory + file don't exist yet
    records = list(store.read_polls())

    # Assert — returns empty rather than raising
    assert records == []


def test_ledger_round_trip(store: StateStore) -> None:
    entry = LedgerEntry(
        ts=datetime(2026, 5, 8, 0, 55, 44, tzinfo=timezone.utc),
        repo="frankyxhl/trinity",
        pr=66,
        head_sha="84d642a5e11715680a9956c20601ed709eeb995c",
        action=LedgerAction.SUBMIT_REVIEW_APPROVE,
        actor="frankyxhl",
        authorized_by="maintainer (interactive --reason='one-shot test')",
        reason="CI green, Codex 👍",
        evidence={"verdict": "ready"},
        result={"reviewDecision": "APPROVED"},
    )
    store.append_ledger(entry)
    store.append_ledger(entry.model_copy(update={"action": LedgerAction.EDIT_PR_BODY_CHECK_BOXES}))

    out = store.read_ledger("frankyxhl/trinity", 66)
    assert len(out) == 2
    assert out[0].action == LedgerAction.SUBMIT_REVIEW_APPROVE
    assert out[1].action == LedgerAction.EDIT_PR_BODY_CHECK_BOXES


def test_ledger_read_returns_empty_when_missing(store: StateStore) -> None:
    assert store.read_ledger("frankyxhl/nope", 1) == []


def test_ledger_accepts_legacy_extra_fields(store: StateStore) -> None:
    """Manually-written ledger entries (pre-CHG-1104) used top-level extra keys
    like `boxes_flipped` / `diff_lines_changed`. Extra='allow' must keep them readable."""
    legacy_path = store._ledger_path("legacy/repo", 7)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        '{"ts":"2026-05-08T00:57:42Z","repo":"legacy/repo","pr":7,"head_sha":"abc","'
        'action":"edit_pr_body_check_boxes","actor":"frankyxhl",'
        '"authorized_by":"maintainer","reason":"r","boxes_flipped":["A12"],"diff_lines_changed":4}\n'
    )
    out = store.read_ledger("legacy/repo", 7)
    assert len(out) == 1
    assert out[0].action == LedgerAction.EDIT_PR_BODY_CHECK_BOXES
    # Extra fields preserved on the model via extra='allow'
    assert getattr(out[0], "boxes_flipped", None) == ["A12"]


def test_append_poll_skips_blank_lines(store: StateStore, pending_poll: PollRecord) -> None:
    # Arrange — manually inject blank lines into the JSONL file
    store.append_poll(pending_poll)
    polls_path = store._polls_path(pending_poll.repo, pending_poll.pr)
    with polls_path.open("a") as f:
        f.write("\n   \n")  # blanks should be tolerated
    store.append_poll(pending_poll)

    # Act
    records = list(store.read_polls())

    # Assert — blanks ignored, both polls returned
    assert len(records) == 2

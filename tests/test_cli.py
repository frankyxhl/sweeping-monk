"""Unit tests for the typer CLI — uses CliRunner against a tmp state dir."""
from __future__ import annotations

from typer.testing import CliRunner

from swm.cli import app
from swm.models import PollRecord, ThreadSnapshot
from swm.state import StateStore

runner = CliRunner()


def _seed(store: StateStore, polls: list[PollRecord], snapshots: list[ThreadSnapshot] | None = None) -> None:
    for p in polls:
        store.append_poll(p)
    for s in snapshots or []:
        store.write_thread(s)


def test_dashboard_command_renders_panel(
    store: StateStore, ready_poll: PollRecord, thread_snapshot: ThreadSnapshot
) -> None:
    # Arrange
    _seed(store, [ready_poll], [thread_snapshot])

    # Act
    result = runner.invoke(app, ["dashboard", ready_poll.repo, "--state-dir", str(store.directory)])

    # Assert
    assert result.exit_code == 0
    assert "owner/repo #49" in result.stdout
    assert "READY" in result.stdout
    assert "Keep required Test checks satisfiable" in result.stdout


def test_dashboard_command_exits_nonzero_when_repo_unknown(store: StateStore) -> None:
    # Arrange — empty store

    # Act
    result = runner.invoke(app, ["dashboard", "owner/repo", "--state-dir", str(store.directory)])

    # Assert
    assert result.exit_code == 1
    assert "no recorded polls" in result.stdout


def test_history_command_filters_by_pr(
    store: StateStore, pending_poll: PollRecord, ready_poll: PollRecord
) -> None:
    # Arrange — two PRs
    other = pending_poll.model_copy(update={"pr": 50, "trigger": "other-pr"})
    _seed(store, [pending_poll, ready_poll, other])

    # Act
    result = runner.invoke(app, [
        "history", pending_poll.repo, "--pr", "49", "--state-dir", str(store.directory),
    ])

    # Assert
    assert result.exit_code == 0
    assert "initial-scan" in result.stdout
    assert "other-pr" not in result.stdout  # PR 50 filtered out


def test_history_command_returns_one_when_no_polls(store: StateStore) -> None:
    # Act
    result = runner.invoke(app, ["history", "owner/repo", "--state-dir", str(store.directory)])

    # Assert
    assert result.exit_code == 1
    assert "no recorded polls" in result.stdout


def test_summary_command_lists_all_open_prs(
    store: StateStore, pending_poll: PollRecord, ready_poll: PollRecord
) -> None:
    # Arrange — two PRs in same repo
    pr_50 = pending_poll.model_copy(update={"pr": 50})
    _seed(store, [ready_poll, pr_50])

    # Act
    result = runner.invoke(app, ["summary", pending_poll.repo, "--state-dir", str(store.directory)])

    # Assert
    assert result.exit_code == 0
    assert "#49" in result.stdout
    assert "#50" in result.stdout


def test_summary_command_returns_one_when_no_polls(store: StateStore) -> None:
    # Act
    result = runner.invoke(app, ["summary", "owner/repo", "--state-dir", str(store.directory)])

    # Assert
    assert result.exit_code == 1


def test_poll_command_runs_against_injected_gh_client(
    store: StateStore, monkeypatch
) -> None:
    """Patch GhClient at the cli module so the typer command never shells out."""
    # Arrange
    from tests.conftest import FakeGhClient
    fake = FakeGhClient(prs=[{
        "number": 49, "title": "demo", "headRefOid": "deadbeef" + "0" * 32,
        "baseRefName": "main", "isDraft": False, "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}],
        "updatedAt": "2026-05-07T12:00:00Z",
    }])
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)

    # Act
    result = runner.invoke(app, ["poll", "owner/repo", "--state-dir", str(store.directory)])

    # Assert
    assert result.exit_code == 0
    assert "owner/repo #49" in result.stdout
    # PollRecord persisted to JSONL
    assert len(list(store.read_polls())) == 1


def test_poll_command_says_no_open_prs_when_repo_is_empty(
    store: StateStore, monkeypatch
) -> None:
    # Arrange
    from tests.conftest import FakeGhClient
    fake = FakeGhClient(prs=[])
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)

    # Act
    result = runner.invoke(app, ["poll", "owner/repo", "--state-dir", str(store.directory)])

    # Assert
    assert result.exit_code == 0
    assert "no open PRs" in result.stdout


# --- SWM-1104 guarded subcommand tests --------------------------------------


SAMPLE_PR_BODY = """## Test plan

- [ ] CI ubuntu-latest passes
- [ ] CI macos-latest passes
- [ ] Codex GitHub bot review
- [ ] Manual smoke on staging
"""


def test_approve_refuses_when_no_poll(store: StateStore, monkeypatch) -> None:
    from tests.conftest import FakeGhClient
    fake = FakeGhClient(
        prs=[{"number": 66, "headRefOid": "abc12345" + "0" * 32, "author": {"login": "ryosaeba1985"}}],
        active_login="frankyxhl",
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "approve", "owner/repo", "66",
        "--reason", "test",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 1
    assert "no recorded poll" in result.stdout


def test_approve_refuses_on_self_action(store: StateStore, monkeypatch, ready_poll: PollRecord) -> None:
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 66})
    store.append_poll(poll)
    fake = FakeGhClient(
        prs=[{"number": 66, "headRefOid": poll.head_sha, "author": {"login": "ryosaeba1985"}}],
        active_login="ryosaeba1985",
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "approve", "owner/repo", "66",
        "--reason", "test",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 1
    assert "self-approval" in result.stdout


def test_approve_refuses_on_stale_head_sha(store: StateStore, monkeypatch, ready_poll: PollRecord) -> None:
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 66})
    store.append_poll(poll)
    fake = FakeGhClient(
        prs=[{"number": 66, "headRefOid": "newhead123" + "0" * 30, "author": {"login": "someone"}}],
        active_login="frankyxhl",
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "approve", "owner/repo", "66",
        "--reason", "test",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 1
    assert "re-poll first" in result.stdout


def test_approve_happy_path_writes_ledger(store: StateStore, monkeypatch, ready_poll: PollRecord) -> None:
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 66, "codex_pr_body_signal": "approved"})
    store.append_poll(poll)
    fake = FakeGhClient(
        prs=[{
            "number": 66, "headRefOid": poll.head_sha,
            "author": {"login": "ryosaeba1985"},
            "reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN",
        }],
        active_login="frankyxhl",
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "approve", "owner/repo", "66",
        "--reason", "CI green + Codex 👍, head fresh",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 0
    assert "APPROVED" in result.stdout
    submitted = [c for c in fake.calls if c[0] == "submit_review_approve"]
    assert len(submitted) == 1
    ledger = store.read_ledger("owner/repo", 66)
    assert len(ledger) == 1
    assert ledger[0].head_sha == poll.head_sha


def test_approve_does_not_ledger_when_review_call_fails(
    store: StateStore, monkeypatch, ready_poll: PollRecord
) -> None:
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 66})
    store.append_poll(poll)
    fake = FakeGhClient(
        prs=[{"number": 66, "headRefOid": poll.head_sha, "author": {"login": "ryosaeba1985"}}],
        active_login="frankyxhl",
        review_should_fail=True,
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "approve", "owner/repo", "66",
        "--reason", "test",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 1
    assert store.read_ledger("owner/repo", 66) == []


def test_tick_flips_only_satisfied_boxes(store: StateStore, monkeypatch, ready_poll: PollRecord) -> None:
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 66, "codex_pr_body_signal": "approved"})
    store.append_poll(poll)
    fake = FakeGhClient(
        prs=[{"number": 66, "headRefOid": poll.head_sha, "author": {"login": "ryosaeba1985"}}],
        active_login="frankyxhl",
        pr_bodies={66: SAMPLE_PR_BODY},
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "tick", "owner/repo", "66",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 0
    edits = [c for c in fake.calls if c[0] == "edit_pr_body"]
    assert len(edits) == 1
    new_body = edits[0][2]["body"]
    # 3 of 4 boxes should be flipped (manual smoke skipped)
    assert new_body.count("- [x]") == 3
    assert "- [ ] Manual smoke" in new_body
    ledger = store.read_ledger("owner/repo", 66)
    assert len(ledger) == 1
    assert ledger[0].evidence["boxes_flipped"][0]["rule"] in {"ci.ubuntu", "ci.macos", "codex.review"}


def test_tick_no_op_when_no_unchecked_boxes(store: StateStore, monkeypatch, ready_poll: PollRecord) -> None:
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 66})
    store.append_poll(poll)
    fake = FakeGhClient(
        prs=[{"number": 66, "headRefOid": poll.head_sha, "author": {"login": "ryosaeba1985"}}],
        active_login="frankyxhl",
        pr_bodies={66: "# all done\n\n- [x] one\n- [x] two\n"},
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "tick", "owner/repo", "66",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 0
    assert "no unchecked boxes" in result.stdout
    assert [c for c in fake.calls if c[0] == "edit_pr_body"] == []


def test_tick_no_flippable_when_only_unverifiable_boxes(
    store: StateStore, monkeypatch, ready_poll: PollRecord
) -> None:
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 66})
    store.append_poll(poll)
    body = "- [ ] Manual review\n- [ ] Stakeholder approval\n"
    fake = FakeGhClient(
        prs=[{"number": 66, "headRefOid": poll.head_sha, "author": {"login": "ryosaeba1985"}}],
        active_login="frankyxhl",
        pr_bodies={66: body},
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "tick", "owner/repo", "66",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 0
    assert "nothing to flip" in result.stdout
    assert [c for c in fake.calls if c[0] == "edit_pr_body"] == []


def test_ledger_command_renders_table(store: StateStore, ready_poll: PollRecord) -> None:
    from datetime import datetime, timezone
    from swm.models import LedgerAction, LedgerEntry
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 66})
    entry = LedgerEntry(
        ts=datetime(2026, 5, 8, 0, 55, 44, tzinfo=timezone.utc),
        repo=poll.repo, pr=poll.pr, head_sha=poll.head_sha,
        action=LedgerAction.SUBMIT_REVIEW_APPROVE,
        actor="frankyxhl", authorized_by="maintainer",
        reason="CI green",
    )
    store.append_ledger(entry)
    result = runner.invoke(
        app, ["ledger", "owner/repo", "66", "--state-dir", str(store.directory)],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0
    assert "submit_review_approve" in result.stdout
    assert "CI green" in result.stdout


def test_ledger_command_says_empty_when_no_entries(store: StateStore) -> None:
    result = runner.invoke(app, ["ledger", "owner/repo", "66", "--state-dir", str(store.directory)])
    assert result.exit_code == 0
    assert "no ledger entries" in result.stdout

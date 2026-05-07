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

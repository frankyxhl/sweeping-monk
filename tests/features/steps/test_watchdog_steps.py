"""BDD step definitions for tests/features/watchdog.feature.

Reuses unit-test fixtures (pending_poll, ready_poll) from the parent conftest.
Steps drive the typer CLI so the feature exercises the real command path.
"""
from __future__ import annotations

from pytest_bdd import given, parsers, scenarios, then, when
from typer.testing import CliRunner

from swm.cli import app
from swm.models import PollRecord
from swm.state import StateStore

scenarios("watchdog.feature")

runner = CliRunner()


@given("a clean state directory", target_fixture="state")
def _clean_state(store: StateStore) -> dict:
    """Use the existing `store` fixture (rooted at tmp_path) — guaranteed empty."""
    return {"store": store, "result": None}


@given(parsers.parse("a poll for PR #{pr:d} with status pending and 1 open Codex thread"))
def _seed_pending(state: dict, pending_poll: PollRecord, pr: int) -> None:
    poll = pending_poll if pending_poll.pr == pr else pending_poll.model_copy(update={"pr": pr})
    state["store"].append_poll(poll)
    state["repo"] = poll.repo


@given(parsers.parse("a later poll for PR #{pr:d} with status ready and the thread resolved"))
def _seed_ready(state: dict, ready_poll: PollRecord, pr: int) -> None:
    poll = ready_poll if ready_poll.pr == pr else ready_poll.model_copy(update={"pr": pr})
    state["store"].append_poll(poll)


@given(parsers.parse("{count:d} sequential polls for PR #{pr:d} where the middle one is identical to the first"))
def _seed_identical_run(state: dict, pending_poll: PollRecord, ready_poll: PollRecord, count: int, pr: int) -> None:
    assert count == 3, "this step is hardcoded for 3 polls"
    duplicate = pending_poll.model_copy(update={"summary": "no-change", "trigger": "no-change"})
    state["store"].append_poll(pending_poll)
    state["store"].append_poll(duplicate)
    state["store"].append_poll(ready_poll)
    state["repo"] = pending_poll.repo


@given(parsers.parse("a poll for PR #{pr:d} where Codex bot is still reviewing"))
def _seed_codex_reviewing(state: dict, pending_poll: PollRecord, pr: int) -> None:
    poll = pending_poll.model_copy(update={
        "pr": pr, "codex_pr_body_signal": "reviewing",
    })
    state["store"].append_poll(poll)
    state["repo"] = poll.repo


@given(parsers.parse("a poll for PR #{pr:d} where Codex bot signaled approval and there are no findings"))
def _seed_codex_approved_no_findings(state: dict, ready_poll: PollRecord, pr: int) -> None:
    poll = ready_poll.model_copy(update={
        "pr": pr, "codex_pr_body_signal": "approved",
        "codex_open": 0, "codex_resolved": 0, "threads": [],
    })
    state["store"].append_poll(poll)
    state["repo"] = poll.repo


@when("the maintainer runs the dashboard command")
def _run_dashboard(state: dict) -> None:
    state["result"] = runner.invoke(app, [
        "dashboard", state["repo"], "--state-dir", str(state["store"].directory),
    ])


@when("the maintainer runs the history command")
def _run_history(state: dict) -> None:
    state["result"] = runner.invoke(app, [
        "history", state["repo"], "--state-dir", str(state["store"].directory),
    ])


@then(parsers.parse("the output shows status {status}"))
def _assert_status(state: dict, status: str) -> None:
    result = state["result"]
    assert result.exit_code == 0, f"command failed: {result.stdout}"
    assert status in result.stdout, f"expected {status!r} in:\n{result.stdout}"


@then(parsers.parse('the output mentions "{phrase}"'))
def _assert_phrase(state: dict, phrase: str) -> None:
    result = state["result"]
    assert phrase in result.stdout, f"expected {phrase!r} in:\n{result.stdout}"


@then(parsers.parse("the timeline shows exactly {n:d} status transitions"))
def _assert_transition_count(state: dict, n: int) -> None:
    """Count the timestamp rows in the rendered table.

    history_table prints one row per state-key change; the BDD scenario seeds 3
    polls where polls 1 and 2 share a state_key, so we expect 2 transitions.
    Detect rows by counting timestamp strings (the records share a date).
    """
    result = state["result"]
    assert result.exit_code == 0, f"command failed: {result.stdout}"
    # Each row begins with a timestamp prefix "2026-05-07T".
    transitions = result.stdout.count("2026-05-07T")
    assert transitions == n, f"expected {n} transitions, saw {transitions} in:\n{result.stdout}"

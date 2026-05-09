"""Unit tests for the typer CLI — uses CliRunner against a tmp state dir."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def _open_pr(number: int, *, head: str | None = None, base: str = "main") -> dict:
    return {
        "number": number,
        "title": f"demo #{number}",
        "headRefOid": head or (f"head{number}" + "0" * 34),
        "baseRefName": base,
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}],
        "updatedAt": "2026-05-07T12:00:00Z",
    }


def _codex_review_thread(
    thread_id: str,
    *,
    is_outdated: bool = True,
    is_resolved: bool = False,
) -> dict:
    return {
        "id": thread_id,
        "isResolved": is_resolved,
        "isOutdated": is_outdated,
        "path": "app.py",
        "line": 12,
        "comments": {
            "nodes": [{
                "databaseId": 1001,
                "author": {"login": "chatgpt-codex-connector"},
                "body": "**P3** stale docs concern",
                "createdAt": "2026-05-07T12:00:00Z",
                "replyTo": None,
            }],
        },
    }


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


def test_close_items_command_resolves_only_target_pr(store: StateStore, monkeypatch) -> None:
    from tests.conftest import FakeGhClient
    from swm.investigator import InvestigationDecision

    class FakeInvestigator:
        def investigate(self, item):
            return InvestigationDecision(
                verdict="RESOLVED",
                confidence=0.91,
                reason="the current diff removes the stale threshold guidance",
                evidence=["the diff no longer includes the fewer-than-3-round skip rule"],
            )

    fake = FakeGhClient(
        prs=[_open_pr(49), _open_pr(50)],
        review_threads={
            49: [_codex_review_thread("PRRT_49")],
            50: [_codex_review_thread("PRRT_50")],
        },
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    monkeypatch.setattr("swm.cli.build_investigator_from_env", lambda: FakeInvestigator())

    result = runner.invoke(app, [
        "close-items", "owner/repo", "49",
        "--yes",
        "--state-dir", str(store.directory),
    ])

    assert result.exit_code == 0, result.stdout
    write_calls = [
        call for call in fake.calls
        if call[0] in {
            "set_review_comment_reaction",
            "reply_to_review_comment",
            "resolve_thread",
            "create_issue_comment",
        }
    ]
    assert [call[0] for call in write_calls] == [
        "set_review_comment_reaction",
        "reply_to_review_comment",
        "resolve_thread",
        "create_issue_comment",
    ]
    assert write_calls[0] == ("set_review_comment_reaction", ("owner/repo", 1001, "+1"), {})
    reply = write_calls[1]
    assert reply[1][:3] == ("owner/repo", 49, 1001)
    reply_body = reply[2]["body"]
    assert "OpenClaw Flash" in reply_body
    assert "the current diff removes the stale threshold guidance" in reply_body
    assert "fewer-than-3-round skip rule" in reply_body
    assert write_calls[2] == ("resolve_thread", ("PRRT_49",), {})
    run_comment = write_calls[3]
    assert run_comment[1][:2] == ("owner/repo", 49)
    assert "Clearance run conclusion" in run_comment[2]["body"]
    assert "Closed this run: `1`" in run_comment[2]["body"]
    record = store.latest_poll("owner/repo", 49)
    assert record is not None
    assert record.trigger == "manual-close-items+stage1.5-sync"
    assert record.stage15_actions[0].threadId == "PRRT_49"
    assert record.stage15_actions[0].result["close_reason_comment"]["id"] == 1002
    assert store.latest_poll("owner/repo", 50) is None


def test_close_items_command_reports_when_nothing_is_closable(store: StateStore, monkeypatch) -> None:
    from tests.conftest import FakeGhClient

    fake = FakeGhClient(
        prs=[_open_pr(49)],
        review_threads={49: [_codex_review_thread("PRRT_49", is_resolved=True)]},
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)

    result = runner.invoke(app, [
        "close-items", "owner/repo", "49",
        "--yes",
        "--state-dir", str(store.directory),
    ])

    assert result.exit_code == 0, result.stdout
    assert "no open review items" in result.stdout
    assert [call for call in fake.calls if call[0] == "resolve_thread"] == []
    comment_calls = [call for call in fake.calls if call[0] == "create_issue_comment"]
    assert len(comment_calls) == 1
    assert "Closed this run: `0`" in comment_calls[0][2]["body"]
    record = store.latest_poll("owner/repo", 49)
    assert record is not None
    assert record.trigger == "manual-close-items"


def test_close_items_command_can_use_configured_github_app_actor(store: StateStore, monkeypatch) -> None:
    from tests.conftest import FakeGhClient
    from swm.investigator import InvestigationDecision

    class FakeInvestigator:
        def investigate(self, item):
            return InvestigationDecision(
                verdict="RESOLVED",
                confidence=0.93,
                reason="verified by app actor test",
                evidence=["evidence from the current diff"],
            )

    class FakeTokenProvider:
        def token_for(self, **kwargs):
            captured["token_kwargs"] = kwargs
            return "installation-token"

    fake = FakeGhClient(
        prs=[_open_pr(49)],
        review_threads={49: [_codex_review_thread("PRRT_49")]},
    )
    captured = {}

    def fake_load_config(path):
        captured["config_path"] = path
        actor = SimpleNamespace(
            app_id=123,
            installation_id=456,
            private_key_path=Path("/tmp/clearance.pem"),
            bot_login="iterwheel-clearance[bot]",
            api_url="https://api.github.com",
        )
        return SimpleNamespace(actor=lambda name: actor)

    def fake_gh_client(*args, **kwargs):
        captured["gh_kwargs"] = kwargs
        return fake

    monkeypatch.setattr("swm.cli.load_config", fake_load_config)
    monkeypatch.setattr("swm.cli.InstallationTokenProvider", FakeTokenProvider)
    monkeypatch.setattr("swm.cli.GhClient", fake_gh_client)
    monkeypatch.setattr("swm.cli.build_investigator_from_env", lambda: FakeInvestigator())

    result = runner.invoke(app, [
        "close-items", "owner/repo", "49",
        "--actor", "clearance",
        "--config", "/tmp/watchd.toml",
        "--yes",
        "--state-dir", str(store.directory),
    ])

    assert result.exit_code == 0, result.stdout
    assert captured["config_path"] == "/tmp/watchd.toml"
    assert captured["token_kwargs"]["app_id"] == 123
    assert captured["token_kwargs"]["installation_id"] == 456
    assert captured["gh_kwargs"] == {
        "token": "installation-token",
        "actor_login": "iterwheel-clearance[bot]",
    }
    assert [call[0] for call in fake.calls if call[0] in {
        "set_review_comment_reaction",
        "reply_to_review_comment",
        "resolve_thread",
        "create_issue_comment",
    }] == [
        "set_review_comment_reaction",
        "reply_to_review_comment",
        "resolve_thread",
        "create_issue_comment",
    ]


def test_close_items_command_marks_unresolved_items_without_resolving(store: StateStore, monkeypatch) -> None:
    from tests.conftest import FakeGhClient

    fake = FakeGhClient(
        prs=[_open_pr(49)],
        review_threads={49: [_codex_review_thread("PRRT_49", is_outdated=False)]},
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)

    result = runner.invoke(app, [
        "close-items", "owner/repo", "49",
        "--yes",
        "--state-dir", str(store.directory),
    ])

    assert result.exit_code == 0, result.stdout
    write_calls = [
        call for call in fake.calls
        if call[0] in {
            "set_review_comment_reaction",
            "reply_to_review_comment",
            "resolve_thread",
            "create_issue_comment",
        }
    ]
    assert [call[0] for call in write_calls] == [
        "set_review_comment_reaction",
        "reply_to_review_comment",
        "create_issue_comment",
    ]
    assert write_calls[0] == ("set_review_comment_reaction", ("owner/repo", 1001, "-1"), {})
    reply_body = write_calls[1][2]["body"]
    assert "Verdict: `OPEN`" in reply_body
    assert "left open" in reply_body
    assert "Closed this run: `0`" in write_calls[2][2]["body"]


def test_close_items_aborts_when_head_drifts_after_confirmation(store: StateStore, monkeypatch) -> None:
    """TOCTOU guard: close-items must re-read head SHA after confirmation and abort on drift."""
    from tests.conftest import FakeGhClient
    from swm.investigator import InvestigationDecision

    class FakeInvestigator:
        def investigate(self, item):
            return InvestigationDecision(verdict="RESOLVED", confidence=0.9, reason="ok", evidence=[])

    class DriftingGh(FakeGhClient):
        def view_pr(self, repo, pr, fields):
            self._record("view_pr", repo, pr, fields=fields)
            view_calls = sum(1 for c in self.calls if c[0] == "view_pr")
            # close-items issues exactly one view_pr (the TOCTOU recheck); drift on it.
            if view_calls >= 1 and "headRefOid" in fields:
                return {**next(p for p in self._prs if p["number"] == pr), "headRefOid": "drifted99" + "0" * 31}
            return next(p for p in self._prs if p["number"] == pr)

    fake = DriftingGh(
        prs=[_open_pr(49)],
        review_threads={49: [_codex_review_thread("PRRT_49")]},
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    monkeypatch.setattr("swm.cli.build_investigator_from_env", lambda: FakeInvestigator())

    result = runner.invoke(app, [
        "close-items", "owner/repo", "49", "--yes",
        "--state-dir", str(store.directory),
    ])

    assert result.exit_code == 1
    assert "head changed since poll" in result.stdout or "re-run close-items" in result.stdout
    write_calls = [c for c in fake.calls if c[0] in {"set_review_comment_reaction", "resolve_thread"}]
    assert write_calls == [], "no writebacks should occur when head drifts"


def test_close_items_require_flash_preflights_all_threads_before_any_mutation(store: StateStore, monkeypatch) -> None:
    """--require-flash must validate ALL threads before any write; no partial mutation."""
    from tests.conftest import FakeGhClient

    threads = [
        _codex_review_thread("PRRT_A"),
        _codex_review_thread("PRRT_B"),
    ]
    fake = FakeGhClient(prs=[_open_pr(49)], review_threads={49: threads})
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    monkeypatch.setattr("swm.cli.build_investigator_from_env", lambda: None)

    result = runner.invoke(app, [
        "close-items", "owner/repo", "49",
        "--require-flash", "--yes",
        "--state-dir", str(store.directory),
    ])

    assert result.exit_code == 1
    assert "no Flash investigator reason" in result.stdout or "refusing to close" in result.stdout
    write_calls = [c for c in fake.calls if c[0] in {"set_review_comment_reaction", "reply_to_review_comment", "resolve_thread"}]
    assert write_calls == [], "no mutations must fire before preflight completes"


def test_close_items_captures_partial_actions_when_second_resolve_fails(store: StateStore, monkeypatch) -> None:
    """actions list must contain the first resolve even when the second resolve raises —
    so the audit comment says 'Closed this run: 1', not '0'."""
    from tests.conftest import FakeGhClient
    from swm.gh import GhCommandError
    from swm.investigator import InvestigationDecision

    class FakeInvestigator:
        def investigate(self, item):
            return InvestigationDecision(verdict="RESOLVED", confidence=0.9, reason="ok", evidence=[])

    resolve_call_count = 0

    class PartialResolveGh(FakeGhClient):
        def resolve_thread(self, thread_id):
            nonlocal resolve_call_count
            resolve_call_count += 1
            self._record("resolve_thread", thread_id)
            if resolve_call_count >= 2:
                raise GhCommandError("simulated second resolve failure")
            return {"resolvedBy": {"login": "iterwheel-clearance[bot]"}}

    threads = [
        _codex_review_thread("PRRT_A"),
        _codex_review_thread("PRRT_B"),
    ]
    fake = PartialResolveGh(prs=[_open_pr(49)], review_threads={49: threads})
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    monkeypatch.setattr("swm.cli.build_investigator_from_env", lambda: FakeInvestigator())

    result = runner.invoke(app, [
        "close-items", "owner/repo", "49", "--yes",
        "--state-dir", str(store.directory),
    ])

    assert result.exit_code == 1
    run_comment_calls = [c for c in fake.calls if c[0] == "create_issue_comment"]
    assert run_comment_calls, "error audit comment must be posted"
    audit_body = run_comment_calls[0][2]["body"]
    assert "Closed this run: `1`" in audit_body, \
        f"expected 'Closed this run: 1' in audit comment, got: {audit_body!r}"


def test_close_items_re_polls_after_confirmation_to_get_fresh_verdicts(store: StateStore, monkeypatch) -> None:
    """After confirmation, close-items must re-poll so thread state that changed
    during the confirmation prompt is not written back with a stale verdict."""
    from tests.conftest import FakeGhClient
    from swm.investigator import InvestigationDecision

    poll_count = 0

    class CountingGh(FakeGhClient):
        def review_threads(self, repo, pr):
            nonlocal poll_count
            poll_count += 1
            return super().review_threads(repo, pr)

    fake = CountingGh(
        prs=[_open_pr(49)],
        review_threads={49: [_codex_review_thread("PRRT_A")]},
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    monkeypatch.setattr("swm.cli.build_investigator_from_env", lambda: None)

    runner.invoke(app, [
        "close-items", "owner/repo", "49", "--yes",
        "--state-dir", str(store.directory),
    ])

    assert poll_count >= 2, (
        "close-items must re-fetch review threads after confirmation "
        f"(review_threads called {poll_count} time(s), expected ≥2)"
    )


def test_close_items_aborts_when_thread_state_drifts_after_confirmation(store: StateStore, monkeypatch) -> None:
    """If a thread's verdict changes between confirmation and re-poll (e.g. a new
    author reply lands during the confirmation prompt), close-items must abort
    rather than silently act on a different plan than the operator confirmed."""
    from tests.conftest import FakeGhClient
    from swm.investigator import InvestigationDecision

    class FakeInvestigator:
        def investigate(self, item):
            return InvestigationDecision(verdict="RESOLVED", confidence=0.9, reason="ok", evidence=[])

    poll_call_count = 0

    class DriftingThreadsGh(FakeGhClient):
        def review_threads(self, repo, pr):
            nonlocal poll_call_count
            poll_call_count += 1
            if poll_call_count == 1:
                # First poll (pre-confirmation): one RESOLVED thread shown in plan.
                return [_codex_review_thread("PRRT_A", is_outdated=True)]
            # Re-poll (post-confirmation): thread is now also resolved by a
            # second thread arriving, changing the closable set.
            return [
                _codex_review_thread("PRRT_A", is_outdated=True),
                _codex_review_thread("PRRT_B", is_outdated=True),
            ]

    fake = DriftingThreadsGh(prs=[_open_pr(49)], review_threads={49: []})
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    monkeypatch.setattr("swm.cli.build_investigator_from_env", lambda: FakeInvestigator())

    result = runner.invoke(app, [
        "close-items", "owner/repo", "49", "--yes",
        "--state-dir", str(store.directory),
    ])

    assert result.exit_code == 1
    assert "Thread state changed" in result.stdout or "re-run close-items" in result.stdout
    write_calls = [c for c in fake.calls if c[0] in {"reply_to_review_comment", "resolve_thread", "set_review_comment_reaction"}]
    assert write_calls == [], f"no writebacks expected after drift abort, got: {write_calls}"


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
        "--reason", "BDD test",
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
        "--reason", "BDD test",
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
        "--reason", "BDD test",
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
        env={"COLUMNS": "300"},
    )
    assert result.exit_code == 0
    assert "submit_review_approve" in result.stdout
    assert "CI green" in result.stdout


def test_ledger_command_says_empty_when_no_entries(store: StateStore) -> None:
    result = runner.invoke(app, ["ledger", "owner/repo", "66", "--state-dir", str(store.directory)])
    assert result.exit_code == 0
    assert "no ledger entries" in result.stdout


# --- SWM-1104 fix-cycle tests (named in CHG §Risks but originally missing) --


def test_approve_refuses_on_head_sha_drift_during_confirmation(
    store: StateStore, monkeypatch, ready_poll: PollRecord
) -> None:
    """TOCTOU mitigation: head SHA must be re-checked between user confirm and gh review.

    The first view_pr returns the verdict's head; the recheck after confirmation
    returns a different head — approve must abort and skip the ledger.
    """
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 66})
    store.append_poll(poll)

    # Custom FakeGhClient that flips headRefOid on the second view_pr call.
    class FlippingGh(FakeGhClient):
        def view_pr(self, repo, pr, fields):
            self._record("view_pr", repo, pr, fields=fields)
            call_index = sum(1 for c in self.calls if c[0] == "view_pr")
            if call_index >= 2 and "headRefOid" in fields:
                # Simulated drift on the recheck.
                return {"number": pr, "headRefOid": "drifted456" + "0" * 30, "author": {"login": "ryosaeba1985"}}
            return {"number": pr, "headRefOid": poll.head_sha, "author": {"login": "ryosaeba1985"}}

    fake = FlippingGh(active_login="frankyxhl")
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "approve", "owner/repo", "66",
        "--reason", "test",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 1
    assert "drifted during confirmation" in result.stdout
    # Crucial: no review submitted, no ledger written
    assert [c for c in fake.calls if c[0] == "submit_review_approve"] == []
    assert store.read_ledger("owner/repo", 66) == []


def test_tick_refuses_on_self_action(store: StateStore, monkeypatch, ready_poll: PollRecord) -> None:
    """Fix #1: tick must respect identity.blocker — self-author can't edit own PR body."""
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 66})
    store.append_poll(poll)
    fake = FakeGhClient(
        prs=[{"number": 66, "headRefOid": poll.head_sha, "author": {"login": "ryosaeba1985"}}],
        active_login="ryosaeba1985",  # same as PR author => self-action
        pr_bodies={66: "- [ ] CI ubuntu-latest passes\n"},
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "tick", "owner/repo", "66",
        "--reason", "should be blocked",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 1
    assert "self-approval" in result.stdout or "self" in result.stdout.lower()
    assert [c for c in fake.calls if c[0] == "edit_pr_body"] == []
    assert store.read_ledger("owner/repo", 66) == []


def test_tick_does_not_ledger_when_body_diff_mismatches(
    store: StateStore, monkeypatch, ready_poll: PollRecord
) -> None:
    """Fix: tick verifies post-edit body matches the prepared diff; if not, refuse to ledger.

    Simulated by a FakeGhClient that pretends to edit but actually returns a different body
    on the verify view_pr call.
    """
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 66, "codex_pr_body_signal": "approved"})
    store.append_poll(poll)
    body = "- [ ] CI ubuntu-latest passes\n"

    class TamperingGh(FakeGhClient):
        def edit_pr_body(self, repo, pr, body):
            self._record("edit_pr_body", repo, pr, body=body)
            # Simulate: gh edit returns success, but the next view_pr shows a DIFFERENT body
            # (e.g. someone else edited concurrently). We MUST refuse to ledger.
            self._pr_bodies[pr] = body + "\n[concurrent edit landed]\n"
            return {"stdout": "edited"}

    fake = TamperingGh(
        prs=[{"number": 66, "headRefOid": poll.head_sha, "author": {"login": "ryosaeba1985"}}],
        active_login="frankyxhl",
        pr_bodies={66: body},
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "tick", "owner/repo", "66",
        "--reason", "test",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 1
    assert "post-edit body does not match" in result.stdout
    # Crucial: edit was attempted (and "succeeded"), but no ledger was written
    assert len([c for c in fake.calls if c[0] == "edit_pr_body"]) == 1
    assert store.read_ledger("owner/repo", 66) == []


def test_approve_does_not_ledger_when_review_call_fails_with_drift_error(
    store: StateStore, monkeypatch, ready_poll: PollRecord
) -> None:
    """Symmetric to test_approve_does_not_ledger_when_review_call_fails but
    distinguishes pre-call drift abort from gh-call-failed abort. Both must
    leave the ledger empty."""
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 66})
    store.append_poll(poll)
    fake = FakeGhClient(
        prs=[{"number": 66, "headRefOid": poll.head_sha, "author": {"login": "ryosaeba1985"}}],
        active_login="frankyxhl",
        review_should_fail=True,  # post-confirmation gh failure
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "approve", "owner/repo", "66",
        "--reason", "test",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 1
    # Review WAS attempted, but ledger must remain empty
    assert len([c for c in fake.calls if c[0] == "submit_review_approve"]) == 1
    assert store.read_ledger("owner/repo", 66) == []


# --- CHG-1105 tick hook + rule-coverage command -----------------------------


SAMPLE_BODY_67 = """## Test plan

- [ ] CI ubuntu-latest passes (no code touched, but workflow may run)
- [ ] CI macos-latest passes
- [ ] Codex GitHub bot review
"""


def test_tick_writes_one_box_miss_per_skipped_classification(
    store: StateStore, monkeypatch, ready_poll: PollRecord
) -> None:
    """Replay PR-#67 round-1 conditions: empty CI + status=PENDING (not trusted)
    yields 2 skipped CI boxes + 1 flipped Codex box → 2 misses written."""
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={
        "repo": "owner/repo", "pr": 66, "ci": {},
        "status": __import__("swm.models", fromlist=["Status"]).Status.PENDING,
        "codex_pr_body_signal": "approved",
    })
    store.append_poll(poll)
    fake = FakeGhClient(
        prs=[{"number": 66, "headRefOid": poll.head_sha, "author": {"login": "ryosaeba1985"}}],
        active_login="frankyxhl",
        pr_bodies={66: SAMPLE_BODY_67},
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "tick", "owner/repo", "66",
        "--reason", "TDD test",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 0, result.stdout
    misses = list(store.read_box_misses("owner/repo"))
    assert len(misses) == 2
    assert {m.box_text for m in misses} == {
        "CI ubuntu-latest passes (no code touched, but workflow may run)",
        "CI macos-latest passes",
    }
    # Both misses must have rule_id set (predicate-refused, not coverage-gap)
    assert all(m.rule_id in {"ci.ubuntu", "ci.macos"} for m in misses)


def test_tick_writes_no_box_misses_when_every_box_flips(
    store: StateStore, monkeypatch, ready_poll: PollRecord
) -> None:
    """Happy negative — when every box satisfies a rule, no misses recorded."""
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 70, "codex_pr_body_signal": "approved"})
    store.append_poll(poll)
    fake = FakeGhClient(
        prs=[{"number": 70, "headRefOid": poll.head_sha, "author": {"login": "ryosaeba1985"}}],
        active_login="frankyxhl",
        pr_bodies={70: "- [ ] CI ubuntu-latest passes\n- [ ] Codex GitHub bot review\n"},
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "tick", "owner/repo", "70",
        "--reason", "TDD test",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 0, result.stdout
    assert list(store.read_box_misses("owner/repo")) == []


def test_tick_writes_box_miss_for_unmatched_rule(
    store: StateStore, monkeypatch, ready_poll: PollRecord
) -> None:
    """Coverage-gap branch: 'CHANGELOG updated' has no BOX_RULES regex.
    The miss is recorded with rule_id=None."""
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 71})
    store.append_poll(poll)
    fake = FakeGhClient(
        prs=[{"number": 71, "headRefOid": poll.head_sha, "author": {"login": "ryosaeba1985"}}],
        active_login="frankyxhl",
        pr_bodies={71: "- [ ] CHANGELOG updated\n- [ ] Manual smoke test\n"},
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "tick", "owner/repo", "71",
        "--reason", "TDD test",
        "--yes",
        "--state-dir", str(store.directory),
    ])
    misses = list(store.read_box_misses("owner/repo"))
    assert len(misses) == 2
    assert all(m.rule_id is None for m in misses)
    texts = {m.box_text for m in misses}
    assert "CHANGELOG updated" in texts
    assert "Manual smoke test" in texts


def test_tick_writes_misses_even_when_user_aborts_confirm(
    store: StateStore, monkeypatch, ready_poll: PollRecord
) -> None:
    """Misses are observations of what the classifier saw, recorded regardless
    of whether the user confirms the flip. Run with input=N to abort confirm."""
    from tests.conftest import FakeGhClient
    poll = ready_poll.model_copy(update={"repo": "owner/repo", "pr": 72, "codex_pr_body_signal": "approved"})
    store.append_poll(poll)
    fake = FakeGhClient(
        prs=[{"number": 72, "headRefOid": poll.head_sha, "author": {"login": "ryosaeba1985"}}],
        active_login="frankyxhl",
        pr_bodies={72: "- [ ] Codex GitHub bot review\n- [ ] Manual smoke test\n"},
    )
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, [
        "tick", "owner/repo", "72",
        "--reason", "TDD test",
        "--state-dir", str(store.directory),
    ], input="n\n")
    # Strict assertions per round-1 review: a future regression that short-circuits
    # before the confirm prompt (e.g. flippable=[] → exit 0) must not silently pass.
    assert result.exit_code == 1
    assert "aborted" in result.stdout.lower()
    misses = list(store.read_box_misses("owner/repo"))
    assert len(misses) == 1
    assert misses[0].box_text == "Manual smoke test"


def _seed_misses(store: StateStore, repo: str, pr: int, *,
                 box_text: str, count: int, rule_id: str | None,
                 ts_offset_days: int = 0) -> None:
    """Helper: write `count` BoxMiss rows for one box_text/rule combo, ts now-offset."""
    from datetime import datetime, timezone, timedelta
    from swm.models import BoxMiss
    base_ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=ts_offset_days)
    for _ in range(count):
        store.append_box_miss(BoxMiss(
            ts=base_ts, repo=repo, pr=pr, head_sha="x" * 40,
            box_text=box_text, rule_id=rule_id, reason="seeded",
        ))


def test_rule_coverage_groups_by_canonical_text(store: StateStore) -> None:
    """count column = number of misses grouped under the canonical (lowercased + ws-collapsed) form."""
    _seed_misses(store, "owner/repo", 1, box_text="CI ubuntu-latest passes", count=3, rule_id="ci.ubuntu")
    _seed_misses(store, "owner/repo", 2, box_text="ci  ubuntu-latest  passes", count=2, rule_id="ci.ubuntu")  # whitespace variant — same canonical
    result = runner.invoke(app, [
        "rule-coverage", "owner/repo",
        "--threshold", "1",
        "--state-dir", str(store.directory),
    ], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout
    # Both variants canonicalize to the same text → one row, count=5
    assert "5" in result.stdout
    assert "ci ubuntu-latest passes" in result.stdout.lower()


def test_rule_coverage_filters_by_threshold(store: StateStore) -> None:
    """Default --threshold 3 hides count<3 rows."""
    _seed_misses(store, "owner/repo", 1, box_text="One-off", count=1, rule_id=None)
    _seed_misses(store, "owner/repo", 2, box_text="Recurring", count=4, rule_id=None)
    result = runner.invoke(app, [
        "rule-coverage", "owner/repo",
        "--state-dir", str(store.directory),
    ], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout
    assert "Recurring".lower() in result.stdout.lower()
    assert "One-off".lower() not in result.stdout.lower()


def test_rule_coverage_filters_by_since_window(store: StateStore) -> None:
    """Default --since 7d filters out misses older than 7 days."""
    _seed_misses(store, "owner/repo", 1, box_text="Old miss", count=4, rule_id=None, ts_offset_days=14)
    _seed_misses(store, "owner/repo", 2, box_text="Recent miss", count=4, rule_id=None, ts_offset_days=1)
    result = runner.invoke(app, [
        "rule-coverage", "owner/repo",
        "--state-dir", str(store.directory),
    ], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout
    assert "Recent miss".lower() in result.stdout.lower()
    assert "Old miss".lower() not in result.stdout.lower()


def test_rule_coverage_distinguishes_predicate_refused_vs_coverage_gap(
    store: StateStore,
) -> None:
    """matched_rule column shows the rule_id for predicate-refused misses,
    or '—' for coverage-gap (no rule matched at all)."""
    _seed_misses(store, "owner/repo", 1, box_text="CI ubuntu-latest passes", count=3, rule_id="ci.ubuntu")
    _seed_misses(store, "owner/repo", 2, box_text="CHANGELOG updated", count=3, rule_id=None)
    result = runner.invoke(app, [
        "rule-coverage", "owner/repo",
        "--state-dir", str(store.directory),
    ], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout
    assert "ci.ubuntu" in result.stdout
    assert "—" in result.stdout  # coverage-gap marker for the CHANGELOG row


def test_rule_coverage_exits_clean_when_no_misses(store: StateStore) -> None:
    """Empty state → friendly message, exit 0."""
    result = runner.invoke(app, [
        "rule-coverage", "never/ran",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 0
    assert "no box misses" in result.stdout.lower()


def test_rule_coverage_canonicalization_keeps_distinct_runner_versions_separate(
    store: StateStore,
) -> None:
    """CHG-1105 Compatibility note: 'CI ubuntu' and 'CI ubuntu-latest' canonicalize
    differently and appear as separate rows. Maintainer decides whether they
    should share a rule. SWM-1106's regex-design step is where that judgement happens."""
    _seed_misses(store, "owner/repo", 1, box_text="CI ubuntu passes", count=3, rule_id="ci.ubuntu")
    _seed_misses(store, "owner/repo", 2, box_text="CI ubuntu-latest passes", count=3, rule_id="ci.ubuntu")
    result = runner.invoke(app, [
        "rule-coverage", "owner/repo",
        "--state-dir", str(store.directory),
    ], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout
    # Two distinct rows, each count=3
    assert result.stdout.count("3") >= 2
    assert "ci ubuntu passes" in result.stdout.lower()
    assert "ci ubuntu-latest passes" in result.stdout.lower()


# --- CHG-1105 round-1 review fixes -----------------------------------------


def test_rule_coverage_threshold_not_hit_exits_clean(store: StateStore) -> None:
    """When misses exist but none hit the threshold, exit 0 with friendly message.
    Branch was uncovered before this test (Claude-as-GLM finding #1)."""
    _seed_misses(store, "owner/repo", 1, box_text="Two-only", count=2, rule_id=None)
    result = runner.invoke(app, [
        "rule-coverage", "owner/repo",
        "--state-dir", str(store.directory),
    ])
    assert result.exit_code == 0
    assert "no patterns hit threshold" in result.stdout.lower()


def test_rule_coverage_invalid_since_aborts_cleanly(tmp_path) -> None:
    """Bad `--since` (e.g. '1w') must surface as a typer.BadParameter clean error,
    not a Python traceback. Round-1 review finding (DeepSeek + Claude-as-GLM)."""
    result = runner.invoke(app, [
        "rule-coverage", "owner/repo",
        "--since", "1w",
        "--state-dir", str(tmp_path / "empty"),
    ])
    # Typer's BadParameter exits with code 2 and prints "Invalid value for ..." to stderr
    assert result.exit_code == 2
    # The exception (if any) should be a typer error, not a bare ValueError
    if result.exception is not None:
        from click.exceptions import BadParameter
        assert isinstance(result.exception, (BadParameter, SystemExit))


# --- CHG-1105 repo argument validation (Codex P2 on PR #1) -----------------


def test_rule_coverage_rejects_malformed_repo_argument(tmp_path) -> None:
    """Codex P2: `swm rule-coverage owner` (no slash) must produce a clean
    typer.BadParameter error (exit 2), not a Python ValueError traceback
    from state.py's repo.split('/', 1)."""
    result = runner.invoke(app, [
        "rule-coverage", "owner",
        "--state-dir", str(tmp_path / "empty-validation"),
    ])
    assert result.exit_code == 2
    if result.exception is not None:
        from click.exceptions import BadParameter
        assert isinstance(result.exception, (BadParameter, SystemExit))


# --- CHG-1107: poll short-circuit CLI render tests -------------------------


def _ready_pr_49() -> dict:
    """A steady-state ready PR for CHG-1107 CLI tests."""
    return {
        "number": 49, "title": "demo", "headRefOid": "deadbeef" + "0" * 32,
        "baseRefName": "main", "isDraft": False, "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}],
        "updatedAt": "2026-05-07T12:00:00Z",
    }


def test_poll_command_renders_full_card_on_first_observation(
    store: StateStore, monkeypatch,
) -> None:
    """CHG-1107 Test D: first observation must render the full dashboard card,
    NOT the short-circuit 'no change:' line."""
    from tests.conftest import FakeGhClient
    fake = FakeGhClient(prs=[_ready_pr_49()])
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)

    result = runner.invoke(app, ["poll", "owner/repo", "--state-dir", str(store.directory)])

    assert result.exit_code == 0
    # Dashboard card rendered (has Rich border characters)
    assert "╭" in result.stdout
    # Short-circuit line must NOT be present
    assert "no change:" not in result.stdout


def test_poll_command_emits_short_circuit_line_when_state_unchanged(
    store: StateStore, monkeypatch,
) -> None:
    """CHG-1107 Test C: second poll with same state emits one-line
    'no change: <repo>#<pr> still <status> @ <short_sha> · codex_open=<k>'
    instead of the full dashboard card. Exit code still 0."""
    from tests.conftest import FakeGhClient
    fake = FakeGhClient(prs=[_ready_pr_49()])
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)

    # First invocation — full card
    result1 = runner.invoke(app, ["poll", "owner/repo", "--state-dir", str(store.directory)])
    assert result1.exit_code == 0
    assert "╭" in result1.stdout
    assert "no change:" not in result1.stdout

    # Second invocation — short-circuit line
    result2 = runner.invoke(app, ["poll", "owner/repo", "--state-dir", str(store.directory)])
    assert result2.exit_code == 0
    # Must contain the short-circuit line
    assert "no change: owner/repo#49 still ready @ " in result2.stdout
    assert "codex_open=0" in result2.stdout
    # Dashboard Rich border characters must be absent
    assert "╭" not in result2.stdout


def test_poll_command_renders_full_cards_when_any_pr_changed_in_multi_pr_run(
    store: StateStore, monkeypatch, ready_poll: PollRecord
) -> None:
    """P1 fix (Codex finding 3207007029): the `no change:` sentinel must represent
    the entire poll run. If any PR in a multi-PR repo changed, the cron-grep
    pattern `grep -q "^no change:" && exit 0` would otherwise lose the alert
    for the changed PR. Rule: emit per-PR `no change:` lines only when EVERY
    outcome in the run is unchanged.
    """
    from tests.conftest import FakeGhClient
    repo = "owner/repo"
    # Seed prior polls for two PRs at known state_keys.
    poll_49 = ready_poll.model_copy(update={"repo": repo, "pr": 49, "head_sha": "stable49" + "0" * 32})
    poll_50 = ready_poll.model_copy(update={"repo": repo, "pr": 50, "head_sha": "stable50" + "0" * 32})
    store.append_poll(poll_49)
    store.append_poll(poll_50)
    # Next run: PR 49 unchanged, PR 50 head bumped (state_key changes).
    fake = FakeGhClient(prs=[
        {"number": 49, "title": poll_49.title, "headRefOid": poll_49.head_sha,
         "baseRefName": "main", "isDraft": False, "mergeStateStatus": poll_49.merge_state,
         "statusCheckRollup": [{"name": k, "conclusion": v.value} for k, v in poll_49.ci.items()],
         "updatedAt": "2026-05-08T00:00:00Z"},
        {"number": 50, "title": poll_50.title, "headRefOid": "newhead50" + "0" * 31,  # bumped
         "baseRefName": "main", "isDraft": False, "mergeStateStatus": poll_50.merge_state,
         "statusCheckRollup": [{"name": k, "conclusion": v.value} for k, v in poll_50.ci.items()],
         "updatedAt": "2026-05-08T01:00:00Z"},
    ])
    monkeypatch.setattr("swm.cli.GhClient", lambda: fake)
    result = runner.invoke(app, ["poll", repo, "--state-dir", str(store.directory)])
    assert result.exit_code == 0, result.stdout
    # Cron-correctness invariant: ANY PR changed → no `no change:` line at line-start
    assert "no change:" not in result.stdout, (
        f"P1: mixed run must NOT emit 'no change:' lines (cron grep would suppress changed PR). "
        f"Got:\n{result.stdout}"
    )
    # Both PRs surface (full cards rendered)
    assert "#49" in result.stdout
    assert "#50" in result.stdout

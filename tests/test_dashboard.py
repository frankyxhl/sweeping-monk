"""Unit tests for dashboard renderers — assert structural content, not exact bytes.

We render to a fixed-width Console with markup disabled and check that key facts
appear (PR number, status icon, head SHA, verdict label, evidence chain).
"""
from __future__ import annotations

from io import StringIO

from rich.console import Console

from swm import dashboard
from swm.models import PollRecord, Status, ThreadSnapshot, Verdict


def render_to_string(renderable, *, width: int = 100) -> str:
    buffer = StringIO()
    console = Console(file=buffer, width=width, force_terminal=False, color_system=None)
    console.print(renderable)
    return buffer.getvalue()


def test_pr_card_shows_status_and_head(ready_poll: PollRecord, thread_snapshot: ThreadSnapshot) -> None:
    # Act
    output = render_to_string(dashboard.pr_card(ready_poll, thread_snapshot))

    # Assert
    assert "owner/repo #49" in output
    assert "READY" in output
    assert "c476c877" in output
    assert "RESOLVED" in output


def test_pr_card_lists_codex_finding_with_evidence(ready_poll: PollRecord, thread_snapshot: ThreadSnapshot) -> None:
    # Act
    output = render_to_string(dashboard.pr_card(ready_poll, thread_snapshot))

    # Assert — evidence chain visible
    assert "Keep required Test checks satisfiable" in output
    assert "P2→P3" in output
    assert "test.yml:31" in output
    assert "abc12345" in output  # commit short sha
    assert "main has no branch protection" in output
    assert "author reply #2002" in output


def test_pr_card_shows_resolved_count_when_all_resolved(ready_poll: PollRecord, thread_snapshot: ThreadSnapshot) -> None:
    # Act
    output = render_to_string(dashboard.pr_card(ready_poll, thread_snapshot))

    # Assert — header lists "1 of 1 resolved" and finding row says "✓ RESOLVED"
    assert "1 of 1 resolved" in output
    assert "RESOLVED" in output


def test_pr_card_shows_open_count_when_thread_unresolved(pending_poll: PollRecord) -> None:
    # Act
    output = render_to_string(dashboard.pr_card(pending_poll))

    # Assert — header lists "0 of 1 resolved" and finding row says "OPEN"
    assert "PENDING" in output
    assert "0 of 1 resolved" in output
    assert "OPEN" in output


def test_summary_table_one_row_per_pr(pending_poll: PollRecord, ready_poll: PollRecord) -> None:
    # Arrange — different PRs
    pr_50 = pending_poll.model_copy(update={"pr": 50})

    # Act
    output = render_to_string(dashboard.summary_table([ready_poll, pr_50]), width=120)

    # Assert
    assert "#49" in output
    assert "#50" in output
    assert "READY" in output
    assert "PENDING" in output


def test_history_table_collapses_no_change_rows(pending_poll: PollRecord, ready_poll: PollRecord) -> None:
    # Arrange — three records: pending, identical-pending, ready
    duplicate = pending_poll.model_copy(update={
        "summary": "another no-change poll",
        "trigger": "poll-cycle-2",
    })
    records = [pending_poll, duplicate, ready_poll]

    # Act
    output = render_to_string(dashboard.history_table(records), width=120)

    # Assert — only 2 status changes shown (pending → ready); duplicate collapsed
    assert "initial-scan" in output
    assert "stage1.5-sync" in output
    assert "poll-cycle-2" not in output  # collapsed because state_key didn't change


def test_history_table_shows_each_status_transition(pending_poll: PollRecord, ready_poll: PollRecord) -> None:
    # Arrange — interleave ready then pending then ready (e.g., regression then re-fix)
    second_pending = pending_poll.model_copy(update={
        "ts": ready_poll.ts.replace(minute=55),
        "trigger": "regression",
    })
    third_ready = ready_poll.model_copy(update={
        "ts": ready_poll.ts.replace(minute=56),
        "trigger": "re-fix",
    })
    records = [pending_poll, ready_poll, second_pending, third_ready]

    # Act
    output = render_to_string(dashboard.history_table(records), width=120)

    # Assert — all 4 transitions visible (none collapsed)
    assert "initial-scan" in output
    assert "stage1.5-sync" in output
    assert "regression" in output
    assert "re-fix" in output


def test_pr_card_handles_missing_snapshot(ready_poll: PollRecord) -> None:
    # Act — render without thread snapshot (StateStore could legit return None)
    output = render_to_string(dashboard.pr_card(ready_poll, snapshot=None))

    # Assert — degrades gracefully, still shows verdict from poll record
    assert "RESOLVED" in output
    assert "owner/repo #49" in output


def test_status_text_reflects_verdict_enum() -> None:
    # Act
    output = render_to_string(dashboard._status_text(Status.BLOCKED))

    # Assert
    assert "BLOCKED" in output


def test_codex_bot_row_says_not_engaged_when_no_reaction_and_no_findings() -> None:
    # Act
    output = render_to_string(dashboard._codex_signal_text(None, has_findings=False))

    # Assert
    assert "not engaged" in output


def test_codex_bot_row_says_left_findings_when_no_reaction_but_threads_present() -> None:
    """Common case when Codex posts inline P2 findings but withholds the body 👍
    until the author addresses them — 'not engaged yet' would be misleading."""
    # Act
    output = render_to_string(dashboard._codex_signal_text(None, has_findings=True))

    # Assert
    assert "left findings" in output
    assert "not engaged" not in output


def test_thread_location_renders_outdated_when_line_is_null() -> None:
    """When GraphQL returns line=None (force-push invalidated the diff anchor)
    we should NOT render 'codex.py:None'."""
    from swm.dashboard import _thread_location

    class _Stub:
        path = "scripts/codex.py"
        line = None

    # Act
    rendered = _thread_location(_Stub())

    # Assert
    assert rendered == "codex.py (outdated)"
    assert "None" not in rendered


def test_thread_location_renders_path_and_line_when_present() -> None:
    from swm.dashboard import _thread_location

    class _Stub:
        path = ".github/workflows/test.yml"
        line = 31

    # Act / Assert
    assert _thread_location(_Stub()) == "test.yml:31"


def test_pr_card_includes_clickable_github_url(ready_poll: PollRecord, thread_snapshot: ThreadSnapshot) -> None:
    """Every report should embed the canonical PR URL so terminals can hyperlink it."""
    # Act
    output = render_to_string(dashboard.pr_card(ready_poll, thread_snapshot))

    # Assert
    expected_url = f"https://github.com/{ready_poll.repo}/pull/{ready_poll.pr}"
    assert expected_url in output


def test_codex_bot_row_prefers_explicit_signal_over_findings_state() -> None:
    # Arrange — has findings AND has a 👀 reaction → reaction wins
    output = render_to_string(dashboard._codex_signal_text("reviewing", has_findings=True))

    # Assert
    assert "reviewing" in output
    assert "left findings" not in output

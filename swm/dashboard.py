"""Rich-based renderers for the watchdog state.

Three views:
- `pr_card(record, snapshot)` — one Panel per PR with Codex thread evidence
- `summary_table(records)` — cross-PR one-row-per-PR table
- `history_table(records)` — chronological status transitions for one PR
"""
from __future__ import annotations

from typing import Iterable

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .models import PollRecord, Status, ThreadSnapshot, Verdict

STATUS_STYLE: dict[Status, tuple[str, str]] = {
    Status.READY:   ("green",  "🟢"),
    Status.BLOCKED: ("red",    "🔴"),
    Status.PENDING: ("yellow", "🟡"),
    Status.ERROR:   ("red",    "⚠️"),
    Status.SKIPPED: ("dim",    "⏭️"),
}
VERDICT_STYLE: dict[Verdict, tuple[str, str]] = {
    Verdict.RESOLVED:             ("green",   "✅"),
    Verdict.OPEN:                 ("yellow",  "🟡"),
    Verdict.NEEDS_HUMAN_JUDGMENT: ("magenta", "🤔"),
}
CI_ICON = {"SUCCESS": "✅", "FAILURE": "❌", "IN_PROGRESS": "⏳", "PENDING": "⏳",
           "CANCELLED": "⏹️", "SKIPPED": "⏭️", "NEUTRAL": "⚪"}
CI_STYLE = {"SUCCESS": "green", "FAILURE": "red", "IN_PROGRESS": "yellow",
            "PENDING": "yellow", "CANCELLED": "red", "SKIPPED": "dim", "NEUTRAL": "dim"}
CODEX_SIGNAL_DISPLAY = {
    "approved":  ("👍 approved",  "green"),
    "reviewing": ("👀 reviewing", "yellow"),
    None:        ("💤 not engaged yet", "dim"),
}
FIELD_LABEL = {
    "Status":    "🚦 Status",
    "Head":      "🔖 Head",
    "CI":        "⚙️ CI",
    "Merge":     "🔀 Merge",
    "Codex bot": "🤖 Codex bot",
    "Findings":  "🔍 Findings",
}

# Maps GitHub mergeStateStatus values (lowercased) to (emoji, style).
MERGE_STATE_DISPLAY = {
    "clean":      ("✅", "green"),
    "unstable":   ("⚠️", "yellow"),
    "blocked":    ("🚫", "red"),
    "behind":     ("⬆️", "yellow"),
    "dirty":      ("❌", "red"),
    "draft":      ("📝", "dim"),
    "has_hooks":  ("✅", "green"),
    "unknown":    ("❔", "dim"),
}


def _merge_text(merge_state: str | None) -> Text:
    if not merge_state:
        return Text("❔ —", style="dim")
    key = merge_state.lower()
    emoji, style = MERGE_STATE_DISPLAY.get(key, ("📌", "dim"))
    return Text(f"{emoji} {key}", style=style)


def _head_text(head_sha: str) -> Text:
    return Text(f"📌 {head_sha[:8]}", style="cyan")


def _status_text(status: Status) -> Text:
    style, icon = STATUS_STYLE.get(status, ("white", "?"))
    return Text(f"{icon} {status.value.upper()}", style=f"bold {style}")


def _ci_text(ci: dict) -> Text:
    """Render CI as 'ubuntu ✓  macos ✓' — labeled, not just dots."""
    text = Text()
    if not ci:
        text.append("(no checks — paths-ignore)", style="dim")
        return text
    for i, (name, conclusion) in enumerate(ci.items()):
        value = conclusion.value if hasattr(conclusion, "value") else str(conclusion)
        if i:
            text.append("  ", style="dim")
        text.append(CI_ICON.get(value, "?"), style=CI_STYLE.get(value, "white"))
        text.append(" ")
        text.append(name, style="dim")
    return text


def _codex_signal_text(signal: str | None) -> Text:
    label, style = CODEX_SIGNAL_DISPLAY.get(signal, ("— unknown", "dim"))
    return Text(label, style=style)


def pr_card(record: PollRecord, snapshot: ThreadSnapshot | None = None) -> Table:
    """Render one PR as a two-column rich Table (Field / Value).
    Border color reflects status; status row is the first entry so it's eye-catching.
    """
    snapshots = {snapshot.thread_id: snapshot} if snapshot else {}
    return _pr_card_with_snapshots(record, snapshots)


def _pr_card_with_snapshots(record: PollRecord, snapshots: dict[str, ThreadSnapshot]) -> Table:
    """Two-column key/value table per PR. Border color tracks status."""
    style, _icon = STATUS_STYLE.get(record.status, ("white", "?"))
    title = Text()
    title.append(f"{record.repo} #{record.pr}", style="bold")
    if record.title:
        title.append("  —  ", style="dim")
        title.append(record.title, style="italic")

    table = Table(
        title=title, title_justify="left",
        show_header=False, box=box.ROUNDED,
        border_style=style, padding=(0, 1),
    )
    table.add_column("field", style="dim", no_wrap=True)
    table.add_column("value")

    table.add_row(FIELD_LABEL["Status"],    _status_text(record.status))
    table.add_row(FIELD_LABEL["Head"],      _head_text(record.head_sha))
    table.add_row(FIELD_LABEL["CI"],        _ci_text(record.ci))
    table.add_row(FIELD_LABEL["Merge"],     _merge_text(record.merge_state))
    table.add_row(FIELD_LABEL["Codex bot"], _codex_signal_text(record.codex_pr_body_signal))

    table.add_section()  # horizontal divider before findings block

    if record.threads:
        total = record.codex_resolved + record.codex_open
        table.add_row(FIELD_LABEL["Findings"], Text(f"{record.codex_resolved} of {total} resolved", style="bold"))
        for i, t in enumerate(record.threads, 1):
            v_style, v_icon = VERDICT_STYLE.get(t.verdict, ("white", "?"))
            head_cell = Text()
            head_cell.append(f"{v_icon} {t.verdict.value}", style=f"bold {v_style}")
            head_cell.append("  ")
            head_cell.append(f"{t.codex_severity.value}→{t.effective_severity.value}", style="bold")
            head_cell.append("  ")
            head_cell.append(f"{t.path.split('/')[-1]}:{t.line}", style="cyan")
            table.add_row(f"  #{i}", head_cell)
            if t.title:
                table.add_row("", Text(t.title))
            for line in _evidence_lines(t, snapshots.get(t.id)):
                table.add_row("", Text(f"• {line}", style="dim"))
    else:
        table.add_row(FIELD_LABEL["Findings"], Text("no Codex inline findings on this head", style="dim"))

    return table


def _evidence_lines(thread, snapshot: ThreadSnapshot | None) -> list[str]:
    """Returns evidence-chain bullets, each prefixed with a type-specific emoji."""
    lines: list[str] = []
    ev = snapshot.evidence if snapshot else None

    reply_id = thread.author_reply_id or (ev and ev.author_reply_id)
    substantive = thread.author_reply_substantive
    if substantive is None and ev:
        substantive = ev.author_reply_substantive
    if reply_id:
        label = "substantive" if substantive else "weak" if substantive is False else "noted"
        lines.append(f"💬 author reply #{reply_id} — {label}")

    commit_sha = thread.new_commit_sha or (ev and ev.code_change_commit)
    if commit_sha:
        summary = (ev.code_change_summary if ev else None) or "code change"
        lines.append(f"📝 commit {commit_sha[:8]} — {summary[:40]}")

    demo = thread.demotion_reason or (ev and ev.demotion_reason)
    if demo:
        lines.append(f"⬇️ severity demoted: {demo}")

    if thread.github_isResolved:
        sync = (ev.synced_via if ev else None) or "GitHub thread resolved"
        lines.append(f"🔗 {sync}")

    return lines


def summary_table(records: Iterable[PollRecord]) -> Table:
    table = Table(title="Open PRs", show_header=True, header_style="bold")
    table.add_column("PR", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("Codex (resolved/total)")
    table.add_column("Head", style="dim")
    table.add_column("Last poll", style="dim")
    for rec in records:
        total = rec.codex_open + rec.codex_resolved
        table.add_row(
            f"#{rec.pr}",
            _status_text(rec.status),
            f"{rec.codex_resolved}/{total} done",
            rec.head_sha[:8],
            rec.ts.isoformat(),
        )
    return table


def history_table(records: list[PollRecord]) -> Table:
    """Show only state transitions — collapse runs of identical state_keys."""
    table = Table(title="Status timeline", show_header=True, header_style="bold")
    table.add_column("Timestamp", style="dim", no_wrap=True)
    table.add_column("Status")
    table.add_column("Codex")
    table.add_column("Head", style="dim")
    table.add_column("Trigger / summary")
    prev_key: tuple | None = None
    for rec in records:
        key = (rec.status, rec.head_sha, rec.codex_open)
        if key == prev_key:
            continue
        prev_key = key
        total = rec.codex_open + rec.codex_resolved
        delta = rec.trigger or (rec.summary or "")[:48]
        table.add_row(
            rec.ts.isoformat(),
            _status_text(rec.status),
            f"{rec.codex_open}/{total}",
            rec.head_sha[:8],
            delta,
        )
    return table

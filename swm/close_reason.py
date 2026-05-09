"""Review-thread conclusion rendering."""
from __future__ import annotations

import os

from .models import Thread, ThreadSnapshot


def has_flash_close_reason(thread: Thread, snapshot: ThreadSnapshot | None) -> bool:
    evidence = snapshot.evidence if snapshot else None
    return bool(thread.llm_reason or (evidence and evidence.llm_reason))


def _clip(value: str, limit: int = 600) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _verdict_value(thread: Thread) -> str:
    return getattr(thread.verdict, "value", thread.verdict)


def conclusion_marker(thread: Thread, *, head_sha: str) -> str:
    return f"swm-thread-conclusion:{thread.id}:{head_sha[:12]}"


def close_reason_marker(thread: Thread, *, head_sha: str) -> str:
    return f"swm-close-reason:{thread.id}:{head_sha[:12]}"


def existing_conclusion_markers(thread: Thread, *, head_sha: str) -> list[str]:
    # For RESOLVED threads, only swm-close-reason satisfies the pre-resolve check.
    # A stale swm-thread-conclusion (left from an earlier OPEN/NHJ marking on the
    # same head) must not substitute — it carries a public "left open" message.
    if _verdict_value(thread) == "RESOLVED":
        return [close_reason_marker(thread, head_sha=head_sha)]
    return [conclusion_marker(thread, head_sha=head_sha)]


def _evidence_lines(thread: Thread, snapshot: ThreadSnapshot | None) -> list[str]:
    evidence = snapshot.evidence if snapshot else None
    if evidence and evidence.llm_evidence:
        return [_clip(item) for item in evidence.llm_evidence[:4]]

    lines: list[str] = []
    if evidence and evidence.thread_state:
        lines.append(f"SWM thread state `{evidence.thread_state}`.")
    author_reply_id = getattr(thread, "author_reply_id", None)
    if author_reply_id:
        lines.append(f"Author reply observed at review comment `{author_reply_id}`.")
    if getattr(thread, "code_changed", None):
        lines.append("Current diff changed after the original review comment.")
    if snapshot and snapshot.github_state and snapshot.github_state.isOutdated:
        lines.append("GitHub marks the original review anchor as outdated on the current head.")
    if not lines:
        lines.append(
            f"SWM's latest poll judged this review thread {_verdict_value(thread)} on the current head."
        )
    return lines


def build_thread_conclusion_comment(
    thread: Thread,
    snapshot: ThreadSnapshot | None,
    *,
    head_sha: str,
    model: str | None = None,
) -> str:
    """Build the public GitHub reply posted under a Codex review thread."""
    evidence = snapshot.evidence if snapshot else None
    flash_reason = thread.llm_reason or (evidence.llm_reason if evidence else None)
    verdict = _verdict_value(thread)
    resolved = verdict == "RESOLVED"
    reason = (
        flash_reason
        or thread.verdict_reason
        or (
            "SWM judged this review thread RESOLVED."
            if resolved
            else "SWM did not find enough evidence to close this review thread."
        )
    )
    model_name = model or os.environ.get("SWM_INVESTIGATOR_MODEL", "deepseek/deepseek-v4-flash")
    verifier = (
        f"OpenClaw Flash (`{model_name}`)"
        if flash_reason
        else "SWM deterministic verifier"
    )
    confidence = thread.llm_confidence or (evidence.llm_confidence if evidence else None)
    confidence_line = f"\n- Confidence: `{confidence:.2f}`" if confidence is not None else ""
    location = f"{thread.path}:{thread.line}" if thread.line else thread.path
    marker_name = (
        close_reason_marker(thread, head_sha=head_sha)
        if resolved
        else conclusion_marker(thread, head_sha=head_sha)
    )
    bullets = "\n".join(f"  - {_clip(item)}" for item in _evidence_lines(thread, snapshot))
    intro = (
        "Clearance verification before resolving this review thread:"
        if resolved
        else "Clearance conclusion for this review thread:"
    )
    result = (
        "The conversation can be resolved now."
        if resolved
        else "The conversation is left open for another author response or human review."
    )
    return (
        f"<!-- {marker_name} -->\n"
        f"{intro}\n\n"
        f"- Verdict: `{verdict}`\n"
        f"- Verified by: {verifier}\n"
        f"- Head: `{head_sha[:12]}`\n"
        f"- Location: `{location}`{confidence_line}\n"
        f"- Why: {_clip(reason)}\n"
        f"- Result: {result}\n"
        "- Evidence:\n"
        f"{bullets}"
    )


def build_close_reason_comment(
    thread: Thread,
    snapshot: ThreadSnapshot | None,
    *,
    head_sha: str,
    model: str | None = None,
) -> str:
    """Build the public GitHub reply posted before resolving a review thread."""
    return build_thread_conclusion_comment(
        thread,
        snapshot,
        head_sha=head_sha,
        model=model,
    )

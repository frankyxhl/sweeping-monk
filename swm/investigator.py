"""Optional LLM investigator for review-thread verdicts.

The investigator never writes to GitHub. It only turns a review-thread evidence
bundle into a structured verdict that poll.py can accept or ignore.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Literal, Protocol

InvestigatorVerdict = Literal["RESOLVED", "OPEN", "NEEDS_HUMAN_JUDGMENT"]


@dataclass(frozen=True)
class ThreadInvestigationInput:
    repo: str
    pr: int
    pr_title: str | None
    head_sha: str
    path: str
    line: int | None
    classification: Literal["B", "C"]
    codex_comment_body: str
    author_reply_body: str | None
    diff_excerpt: str
    heuristic_verdict: str
    heuristic_reason: str


@dataclass(frozen=True)
class InvestigationDecision:
    verdict: InvestigatorVerdict
    confidence: float
    reason: str
    evidence: list[str]
    raw_text: str | None = None


class ThreadInvestigator(Protocol):
    def investigate(self, item: ThreadInvestigationInput) -> InvestigationDecision:
        ...


class InvestigationError(RuntimeError):
    """LLM/integration failure. Callers should fall back to deterministic logic."""


def _truthy(value: str | None) -> bool:
    return bool(value) and value.lower() not in {"0", "false", "no", "off"}


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]..."


def _extract_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return dict(json.loads(stripped))
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.S)
        if not match:
            raise
        return dict(json.loads(match.group(0)))


def _coerce_decision(raw: dict, *, min_confidence: float, raw_text: str) -> InvestigationDecision:
    verdict = str(raw.get("verdict") or "").upper()
    if verdict not in {"RESOLVED", "OPEN", "NEEDS_HUMAN_JUDGMENT"}:
        raise InvestigationError(f"invalid investigator verdict: {verdict!r}")
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise InvestigationError("invalid investigator confidence") from exc
    confidence = max(0.0, min(1.0, confidence))
    reason = str(raw.get("reason") or "").strip()
    if not reason:
        raise InvestigationError("investigator reason is empty")
    evidence_raw = raw.get("evidence")
    evidence = [str(item).strip() for item in evidence_raw if str(item).strip()] if isinstance(evidence_raw, list) else []
    if verdict == "RESOLVED" and confidence < min_confidence:
        verdict = "NEEDS_HUMAN_JUDGMENT"
        reason = f"LLM confidence {confidence:.2f} below threshold {min_confidence:.2f}: {reason}"
    return InvestigationDecision(
        verdict=verdict,  # type: ignore[arg-type]
        confidence=confidence,
        reason=reason,
        evidence=evidence,
        raw_text=raw_text,
    )


class OpenClawInvestigator:
    def __init__(
        self,
        *,
        command: str,
        model: str,
        timeout_seconds: int = 120,
        max_diff_chars: int = 20000,
        min_confidence: float = 0.78,
    ) -> None:
        self.command = command
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_diff_chars = max_diff_chars
        self.min_confidence = min_confidence

    def investigate(self, item: ThreadInvestigationInput) -> InvestigationDecision:
        prompt = self._prompt(item)
        args = [
            *shlex.split(self.command),
            "infer",
            "model",
            "run",
            "--gateway",
            "--model",
            self.model,
            "--prompt",
            prompt,
            "--json",
        ]
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if proc.returncode != 0:
            raise InvestigationError(proc.stderr.strip() or proc.stdout.strip() or "openclaw command failed")
        try:
            envelope = json.loads(proc.stdout)
            text = str((envelope.get("outputs") or [{}])[0].get("text") or "")
            raw = _extract_json_object(text)
        except Exception as exc:
            raise InvestigationError(f"could not parse investigator JSON: {exc}") from exc
        return _coerce_decision(raw, min_confidence=self.min_confidence, raw_text=text)

    def _prompt(self, item: ThreadInvestigationInput) -> str:
        payload = {
            "repo": item.repo,
            "pr": item.pr,
            "pr_title": item.pr_title,
            "head_sha": item.head_sha,
            "thread_location": {"path": item.path, "line": item.line},
            "thread_classification": item.classification,
            "codex_review_comment": item.codex_comment_body,
            "author_reply": item.author_reply_body,
            "heuristic": {
                "verdict": item.heuristic_verdict,
                "reason": item.heuristic_reason,
            },
            "diff_excerpt": _truncate(item.diff_excerpt, self.max_diff_chars),
        }
        return (
            "You are the Clearance investigator for a GitHub PR review thread. "
            "Decide whether the review concern is actually fixed in the current head.\n"
            "Use only the provided PR diff excerpt, review comment, and author reply. "
            "Do not assume fixes that are not evidenced. If the evidence is partial, "
            "ambiguous, outside the diff excerpt, or requires running code, choose "
            "NEEDS_HUMAN_JUDGMENT.\n"
            "Return exactly one JSON object with this schema:\n"
            '{"verdict":"RESOLVED|OPEN|NEEDS_HUMAN_JUDGMENT","confidence":0.0,'
            '"reason":"short factual reason","evidence":["quoted or paraphrased evidence"]}\n'
            "Input:\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )


def build_investigator_from_env() -> ThreadInvestigator | None:
    if not _truthy(os.environ.get("SWM_INVESTIGATOR_ENABLED")):
        return None
    backend = os.environ.get("SWM_INVESTIGATOR_BACKEND", "openclaw").strip().lower()
    if backend != "openclaw":
        raise InvestigationError(f"unsupported SWM_INVESTIGATOR_BACKEND={backend!r}")
    command = os.environ.get("SWM_OPENCLAW_COMMAND", "openclaw")
    model = os.environ.get("SWM_INVESTIGATOR_MODEL", "deepseek/deepseek-v4-flash")
    timeout = int(os.environ.get("SWM_INVESTIGATOR_TIMEOUT_SECONDS", "120"))
    max_diff = int(os.environ.get("SWM_INVESTIGATOR_MAX_DIFF_CHARS", "20000"))
    min_confidence = float(os.environ.get("SWM_INVESTIGATOR_MIN_CONFIDENCE", "0.78"))
    return OpenClawInvestigator(
        command=command,
        model=model,
        timeout_seconds=timeout,
        max_diff_chars=max_diff,
        min_confidence=min_confidence,
    )

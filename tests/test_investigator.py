from __future__ import annotations

import subprocess

import pytest

from swm.investigator import (
    OpenClawInvestigator,
    ThreadInvestigationInput,
    _coerce_decision,
    _extract_json_object,
)


def _input() -> ThreadInvestigationInput:
    return ThreadInvestigationInput(
        repo="owner/repo",
        pr=7,
        pr_title="Fix bug",
        head_sha="abc123",
        path="app.py",
        line=12,
        classification="C",
        codex_comment_body="This leaks the token.",
        author_reply_body="Fixed in app.py by removing token logging.",
        diff_excerpt="diff --git a/app.py b/app.py\n- print(token)\n+ logger.info('ok')\n",
        heuristic_verdict="OPEN",
        heuristic_reason="author reply was too short",
    )


def test_extract_json_object_accepts_fenced_json() -> None:
    assert _extract_json_object('```json\n{"verdict":"OPEN"}\n```') == {"verdict": "OPEN"}


def test_coerce_decision_demotes_low_confidence_resolved() -> None:
    decision = _coerce_decision(
        {
            "verdict": "RESOLVED",
            "confidence": 0.5,
            "reason": "probably fixed",
            "evidence": ["diff changed"],
        },
        min_confidence=0.8,
        raw_text="{}",
    )

    assert decision.verdict == "NEEDS_HUMAN_JUDGMENT"
    assert decision.confidence == 0.5


def test_openclaw_investigator_parses_model_json(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_run(args, *, capture_output, text, timeout):
        captured["args"] = args
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                '{"outputs":[{"text":"{\\"verdict\\":\\"RESOLVED\\",'
                '\\"confidence\\":0.91,\\"reason\\":\\"diff removes token logging\\",'
                '\\"evidence\\":[\\"print(token) removed\\"]}"}]}'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    investigator = OpenClawInvestigator(
        command="/bin/openclaw",
        model="deepseek/deepseek-v4-flash",
        min_confidence=0.8,
    )

    decision = investigator.investigate(_input())

    assert decision.verdict == "RESOLVED"
    assert decision.confidence == 0.91
    assert "deepseek/deepseek-v4-flash" in captured["args"]
    assert "--gateway" in captured["args"]

# CHG-1111: OpenClaw-backed state-change decision adapter for `swm-watchd`

**Applies to:** SWM project
**Last updated:** 2026-05-09
**Last reviewed:** 2026-05-09
**Status:** Proposed
**Date:** 2026-05-09
**Requested by:** Frank Xu / Codex (GitHub issue #7)
**Priority:** P2 — Normal
**Change Type:** Normal
**Depended on by:** SWM-1108 (the daemon imports this adapter library to dispatch state-change LLM calls; SWM-1108's `Depends on: CHG-1111` is the truthful direction). This CHG ships a self-contained library and does NOT itself depend on the daemon — round-2 expressed both directions which Codex review #3212833044 correctly flagged as a circular ordering. The acceptance criterion that this PR also amends SWM-1108's `Targets` line is a co-edit requirement, not a depends-on (the SWM-1108 amendment lives in this PR's commits regardless of merge order).
**Targets:** New `swm/agent_adapter.py` (~80 LoC, Protocol + Pydantic models). New `swm/agent_openclaw.py` (~120 LoC, subprocess runner against the existing local `openclaw` CLI). New `swm/prompts/openclaw_state_change.txt` (package data, JSON-only output template). New tests (~150 LoC). One-line edit to SWM-1108's `Targets` line ("delegates the LLM call to the adapter from CHG-1111" instead of "Sonnet via Anthropic SDK"). pyproject.toml: `anthropic` becomes optional `[project.optional-dependencies] daemon-anthropic` so OpenClaw-only installs pull no Python dep.

---

## What

A library slice: a provider-neutral agent adapter + the OpenClaw implementation. Daemon wiring is owned by SWM-1108, which calls into this library. No `swm/daemon.py` edits ship in this CHG.

```python
# swm/agent_adapter.py
class AdapterError(BaseModel):
    """Returned (not raised) on timeout / non-zero exit / malformed JSON /
    schema validation failure. Caller writes the audit row; the adapter
    has no filesystem responsibility (Codex review #3212833042)."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["timeout","nonzero_exit","malformed_json","schema_violation"]
    detail: str = Field(max_length=400)

class AgentResult(BaseModel):
    """Wraps every `decide()` outcome. Exactly one of `decision` or
    `error` is set. The daemon (which owns StateStore) writes one
    audit row to `agent-decisions.jsonl` per `AgentResult` regardless
    of outcome."""
    model_config = ConfigDict(extra="forbid")
    adapter: str                                # e.g. "openclaw"
    transition: str                             # echoed from AgentEvidence.transition
    latency_ms: int
    decision: AgentDecision | None = None
    error: AdapterError | None = None

class AgentAdapter(Protocol):
    name: str  # "openclaw" | future
    def decide(self, evidence: AgentEvidence, *, timeout_s: float) -> AgentResult: ...
    # NOTE: `decide()` does NOT touch the filesystem. The adapter constructs
    # an `AgentResult` (with `decision` set on success, `error` set on
    # failure) and returns it. The caller — which holds the StateStore
    # appropriate to its `--state-dir` — is responsible for appending the
    # audit row. This keeps the adapter testable without a state directory
    # and respects custom `--state-dir` overrides (Codex review #3212833042).

class AgentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")  # no PollRecord drift via extra fields
    repo: str
    pr: int
    head_sha: str
    status: Status                              # from swm/models.py
    ci: dict[str, CIConclusion]                 # from PollRecord allow-listed
    codex_open: int
    codex_pr_body_signal: Literal["reviewing","approved"] | None
    thread_summaries: list[ThreadSummary]       # NOT raw Codex comment bodies
    transition: Literal["first-seen","head-bumped","ci-flipped","codex-changed","status-changed"]
    prior_state_key: tuple | None
    evidence_refs: list[str]                    # canonical refs the daemon supplied; populated by from_poll()

    @classmethod
    def from_poll(cls, new: PollRecord, prior: PollRecord | None) -> "AgentEvidence":
        """SOLE constructor. Reads ONLY the listed PollRecord fields (closed allow-list).
        Future PollRecord fields added under model_config={'extra':'allow'} cannot leak into
        the prompt because they are not enumerated here. Populates evidence_refs from
        canonical sources only (e.g. 'thread:<id>', 'ci:<check_name>', 'pr_body_signal')."""
        ...

class ThreadSummary(BaseModel):
    """Distilled thread evidence — never the raw GitHub comment body."""
    model_config = ConfigDict(extra="forbid")
    thread_id: str
    path: str
    line: int | None
    effective_severity: Severity                # already promoted per SWM-1102
    verdict: Verdict                            # already classified per SWM-1101
    verdict_reason: str | None                  # sanitized reason from SWM-1101 pipeline

class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["notify","record-only","ignore"]
    severity: Severity                          # P1 | P2 | P3 (existing enum; no widening)
    summary: str = Field(max_length=400)
    suggested_action: Literal["swm approve","swm tick","manual-review","none"]
    suggested_reason: str | None = Field(default=None, max_length=200)
    evidence_refs: list[str]                    # MUST be subset of AgentEvidence.evidence_refs (Pydantic validator)
```

OpenClaw runner (`swm/agent_openclaw.py`):

- Loads the prompt template from package data at `swm/prompts/openclaw_state_change.txt`. Path is overridable per-deployment via `[agent].prompt_template` in `~/.config/swm/watchd.toml`; SWM-1108 owns reading that config and passing the resolved path to `OpenClawAdapter(prompt_template_path=...)`.
- Spawns the existing local `openclaw` CLI via `subprocess.run(..., timeout=timeout_s, capture_output=True, text=True, env=scrubbed_env)` where `scrubbed_env` sets `OPENCLAW_DISABLE_TOOLS=1` and removes `gh` from `PATH` (defense in depth).
- On non-zero exit, timeout, malformed JSON, or schema validation failure: returns `AgentResult(adapter="openclaw", transition=..., latency_ms=..., decision=None, error=AdapterError(kind=..., detail=...))`. The adapter performs no filesystem writes — see the `decide()` protocol note above. The daemon (SWM-1108) writes one row to `agent-decisions.jsonl` (using its own StateStore so `--state-dir` overrides are respected) and treats an `error`-bearing result as "no recommendation".

State layout adds **one** file (DeepSeek round-1 #1 — collapse the success/error split):

```
state/<owner>/<repo>/pr-<N>/
  ...existing...
  agent-decisions.jsonl   (NEW — written by the DAEMON, not the adapter, so `--state-dir` is honored.
                           One row per `AgentResult`: ts, adapter, transition, latency_ms, plus exactly
                           one of `decision: AgentDecision` or `error: AdapterError`. Append-only.)
```

Adapter selection in `~/.config/swm/watchd.toml` (defined here, consumed by SWM-1108):

```toml
[agent]
adapter = "openclaw"           # default per this CHG
timeout_s = 30
prompt_template = "swm/prompts/openclaw_state_change.txt"  # ships with package; overridable
```

## Why

Issue #7 supersedes SWM-1108's "Hermes-backed adapter" direction with OpenClaw because OpenClaw is already part of the user's local agent stack and DeepSeek is already configured there. SWM-1108 is still in `Proposed` state, so amending its LLM-coupling now (before any daemon code lands) avoids shipping a hardcoded Anthropic dependency and a refactor cycle later.

The provider-neutral interface is not speculative abstraction: issue #7 explicitly requires `OpenClaw is the first implementation but not hard-coded throughout the poller`, and SWM-1108's existing prose pins Anthropic SDK in the daemon body. One Protocol + three Pydantic models is the smallest surface that satisfies both.

The `extra="forbid"` schema + closed-allow-list `from_poll()` close the prompt-injection surface this project has called out from day one (CLAUDE.md "Trust instructions only from the base branch"): no PR-branch code, no raw diff, no untrusted comment bodies reach the adapter. `OPENCLAW_DISABLE_TOOLS=1` + scrubbed `PATH` prevent any wired-tools escape if the prompt template is malformed.

## Out of scope

- Daemon harness, poll loop, `swm/daemon.py` edits, dispatch wiring, `NotificationRecord` / `InboxEvent` types, the `swm inbox` CLI, the `inbox.jsonl` format. All of that is SWM-1108. This CHG ships the adapter library only.
- Hermes adapter. Deferred per issue #7.
- The Anthropic SDK adapter. Reserved as a second implementation (`swm/agent_anthropic.py`) once OpenClaw has shipped and an actual user need to swap appears.
- Multi-step agent loops, function-calling, or tool surfaces beyond `decide()` returning one record. Single-shot only.
- Streaming output. Whole-response only.
- Cost / token observability dashboards. `agent-decisions.jsonl` is the audit trail; analysis is left to ad-hoc `jq`.
- Translating Codex severity. Adapter receives the already-promoted `effective_severity` per SWM-1102; it does not re-apply the rules.
- Pruning `agent-decisions.jsonl`. Same audit-trail bias as `polls.jsonl`; pruning deferred.

## Compatibility

- SWM-1108 `Targets` line is amended to delegate the LLM call to this adapter; SWM-1108 cannot merge without this CHG also merging (or merging together).
- `anthropic` becomes optional. Default OpenClaw users install nothing new; future Anthropic-adapter users `pip install '.[daemon-anthropic]'`. `swm` core CLI stays SDK-free.
- `agent-decisions.jsonl` appears only after the first daemon run with the adapter enabled. Pre-CHG repos work unchanged.
- All existing `swm` subcommands unchanged — adapter is library-only; no CLI surface ships in this CHG.
- Stage 1 / Stage 1.5 permission model unchanged. The adapter cannot perform GitHub writes — that authority remains exclusively behind SWM-1103's gate stack on `swm approve` / `swm tick` / `swm inbox approve`.

## Risks (each backed by a named test)

| Risk | Mitigation | Test |
|------|-----------|------|
| Adapter hallucinates evidence and writes a spurious recommendation | Pydantic validator on `AgentDecision.evidence_refs` requires every ref to be a member of the `AgentEvidence.evidence_refs` set the daemon supplied; rejection path produces `AgentResult(error=AdapterError(kind="schema_violation", ...))` | `test_decision_with_unknown_evidence_ref_is_rejected` |
| Future `PollRecord` fields (added under `extra="allow"`) leak PR-branch content into the prompt | `AgentEvidence` uses `extra="forbid"`; `from_poll()` is the SOLE constructor and reads only the closed allow-list of `PollRecord` fields enumerated above; never iterates `PollRecord.__dict__` | `test_agent_evidence_from_poll_ignores_unknown_pollrecord_fields`; `test_agent_evidence_rejects_extra_fields` |
| OpenClaw output is not valid JSON / not schema-compliant | Adapter catches `json.JSONDecodeError` + `ValidationError` and returns `AgentResult(decision=None, error=AdapterError(kind="malformed_json"\|"schema_violation", ...))`; the daemon writes one row to `agent-decisions.jsonl` (so `--state-dir` is honored) | `test_openclaw_malformed_json_returns_adapter_error`; `test_openclaw_schema_violation_returns_adapter_error`; `test_caller_writes_audit_row_for_error_result_under_custom_state_dir` |
| OpenClaw hangs and blocks the caller | `subprocess.run(..., timeout=timeout_s)`; `subprocess.TimeoutExpired` → `AgentResult(error=AdapterError(kind="timeout", ...))` | `test_openclaw_timeout_returns_adapter_error` |
| Adapter writes audit row to default state tree, ignoring caller `--state-dir` | Adapter is filesystem-free by design — `decide()` returns `AgentResult` and the daemon (which holds the `StateStore` matching its `--state-dir`) writes the audit row. A dedicated test passes a `StateStore(tmp_path)` and asserts the row lands under `tmp_path`, not the global default tree (Codex review #3212833042). | `test_caller_writes_audit_row_for_error_result_under_custom_state_dir` (also covers the success path: `test_caller_writes_audit_row_for_ok_result_under_custom_state_dir`) |
| Adapter performs a GitHub write through some indirect path | `OPENCLAW_DISABLE_TOOLS=1` is set on the subprocess env; `gh` removed from `PATH` in the scrubbed env; decision schema has no `github_action` field; test asserts subprocess env contains the disable flag and that `which gh` from inside the scrubbed env returns nothing | `test_adapter_subprocess_env_disables_tools_and_scrubs_gh` |
| Raw Codex comment body or PR diff reaches the adapter prompt | `from_poll()` constructs `ThreadSummary` from `PollRecord.threads` (already-canonicalized SWM-1101 verdicts); test asserts no field of `AgentEvidence` after `from_poll()` contains the substring of a planted raw comment body | `test_agent_evidence_omits_raw_comment_bodies_and_diff` |
| Provider switch breaks downstream consumers | All consumers of the library read `AgentDecision`; switching adapters cannot change the model shape | `test_fake_adapter_produces_identical_decision_shape` |
| Decision `kind="ignore"` writes inconsistent state | The daemon always writes one audit line to `agent-decisions.jsonl` per `AgentResult`; for `ignore` it stops there. For `notify` the daemon also writes a notification; for `record-only` an inbox event. Adapter does not see these surfaces. | `test_ignore_kind_writes_audit_only`; `test_notify_kind_writes_audit` (caller-side notification/inbox write covered by SWM-1108) |

## Acceptance

- [ ] `swm/agent_adapter.py` defines `AgentAdapter` Protocol + `AgentEvidence` (with `extra="forbid"` and a `from_poll` classmethod) + `ThreadSummary` + `AgentDecision` (with `evidence_refs` subset validator) Pydantic models.
- [ ] `swm/agent_openclaw.py` implements `OpenClawAdapter(prompt_template_path: Path)` against the local `openclaw` CLI; default template ships at `swm/prompts/openclaw_state_change.txt` as package data.
- [ ] `from_poll()` reads only the closed allow-list of `PollRecord` fields enumerated in §What. A test plants an unknown extra field on a `PollRecord` instance and asserts it does not appear anywhere in the resulting `AgentEvidence`.
- [ ] `decide()` performs no filesystem writes. A test passes `StateStore(tmp_path)` to the daemon, runs a full success and a full error path, and asserts the audit rows land under `tmp_path` (NOT the default state tree).
- [ ] On valid output, the daemon writes exactly one row to `agent-decisions.jsonl` shaped `{ts, adapter, transition, latency_ms, decision: AgentDecision}`. On invalid output / timeout / non-zero exit, exactly one row shaped `{ts, adapter, transition, latency_ms, error: AdapterError}`. The two shapes are discriminable by which of `decision` / `error` is present (the round-1 single-file collapse with an `outcome` discriminator is replaced by this nullable-pair pattern, same audit-trail bias).
- [ ] Stage-1 audit: across the full test suite, no test invokes `gh api --method POST/PATCH/PUT/DELETE`, `gh pr review`, or `gh pr merge` from an adapter code path. Asserted via a `gh` shim in `conftest.py` that fails the suite if any write verb is attempted from `swm/agent_*`.
- [ ] SWM-1108's `Targets` line is updated in the same PR to delegate the LLM call to this adapter.
- [ ] README adds a one-paragraph block describing OpenClaw-first / Hermes-deferred per issue #7's acceptance criterion 9.
- [ ] `pytest` ≥ 80% gate; new modules ≥ 90% line coverage.
- [ ] `af validate --root .` clean.

## Approval

- [x] Trinity fast-review (preset `{glm, deepseek}` per `~/.claude/trinity.json`; ≥ 9.0 mean, no convergent FAIL). **Round-2 PASS: GLM 9.33, DeepSeek 9.33, mean 9.33 (2026-05-09).**
- [ ] Approved by: <reviewer> on <date>

---

## Change History

| Date | Change | By |
|------|--------|----|
| 2026-05-09 | Initial proposal — provider-neutral agent adapter + OpenClaw first impl, supersedes SWM-1108's hardcoded Sonnet/Anthropic-SDK wiring per issue #7 | Claude Code |
| 2026-05-09 | Round-1 fixes per trinity fast-review (GLM 8.90 / FAIL, DeepSeek 8.88 / FAIL, mean 8.89). Convergent finding: `from_poll()` allow-list. (1) `AgentEvidence.evidence_refs` defined + Pydantic subset validator on `AgentDecision.evidence_refs` (GLM #1). (2) `from_poll()` SOLE constructor with closed `PollRecord` field allow-list + `extra="forbid"` (DS #5 + GLM #2 convergent). (3) Dropped `NotificationRecord` / `InboxEvent` refs — those live in SWM-1108 (GLM #3). (4) Descoped all `swm/daemon.py` edits to SWM-1108; this CHG is library-only (DS #2). (5) Collapsed `agent-errors.jsonl` into `agent-decisions.jsonl` with `outcome` discriminator (DS #1). (6) Moved "Trinity fast-review" criterion from Acceptance to Approval (DS #3). (7) Reconciled prompt-template path/overridability — ships as package data, override via `[agent].prompt_template` (DS #4). (8) Dropped `severity="info"` — `AgentDecision.severity` reuses existing `Severity` enum (P1/P2/P3) (GLM nb). (9) `ignore` kind reconciled — always writes one audit line to `agent-decisions.jsonl`, never to notifications/inbox (GLM nb). (10) Renamed `test_anthropic_stub_adapter_*` to `test_fake_adapter_*` so it doesn't pin the deferred Anthropic adapter (GLM nb). | Claude Code |
| 2026-05-09 | Codex round-3 PR-review fixes on PR #10. (1) Comment #3212833042: `decide()` previously appended directly to `agent-decisions.jsonl`, which forced the global default state tree even when callers used `--state-dir`. **Adapter is now filesystem-free**: `decide()` returns `AgentResult` (wrapping either `AgentDecision` on success or `AdapterError` on failure); the daemon — which holds the `StateStore` matching its `--state-dir` — performs the audit append. New named tests (`test_caller_writes_audit_row_for_*_result_under_custom_state_dir`) lock the contract. (2) Comment #3212833044: removed reciprocal `Depends on: SWM-1108` (kept `Depended on by: SWM-1108`); the SWM-1108 amendment is a co-edit requirement, not an ordering dependency, so the cycle no longer exists. | Claude Code |

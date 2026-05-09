# CHG-1108: `swm-watchd` daemon + `swm inbox` CLI + provider-neutral LLM wiring

**Applies to:** SWM project
**Last updated:** 2026-05-09
**Last reviewed:** 2026-05-09
**Status:** Proposed
**Date:** 2026-05-08
**Requested by:** Frank Xu (interactive session, after CHG-1107 closes the routine-polling token cost — this CHG handles the remaining "state-changed" path so it stops firing Opus too)
**Priority:** Medium-Low (only ship after CHG-1107 has run for ≥ 1 week and proves the routine-polling path is solved)
**Change Type:** Normal
**Depends on:** CHG-1111 (provider-neutral agent adapter — the daemon delegates the LLM call to the adapter from CHG-1111; OpenClaw is the first impl, Anthropic SDK is one possible second impl behind the same interface).
**Targets:** New `swm/daemon.py` (long-running poller, ~150 LoC). New `swm/inbox.py` (event/decision file I/O, ~80 LoC). New `swm/cli.py` subcommands `inbox` family (~120 LoC). The LLM call is delegated to the adapter from CHG-1111 (was previously "New `swm/sdk_client.py` (anthropic SDK wrapper, ~60 LoC)" — superseded; the daemon imports `swm/agent_adapter.py` instead). New `swm-watchd` console_script entry point (~10 LoC). pyproject.toml: `anthropic` is **not** added to required deps; users opting into the Anthropic adapter install via `[project.optional-dependencies] daemon-anthropic` per CHG-1111.

---

## What

Replaces the cron-driven Claude-Code-session model with a long-running Python daemon that polls PRs continuously (configurable cadence, default 60s — sub-minute is fine since polling is now token-free per CHG-1107), and delegates every state-change LLM call to the **provider-neutral adapter from CHG-1111** (default OpenClaw + DeepSeek; Anthropic SDK is one possible second impl behind the same interface).

Three new pieces:

| Piece | Surface | What it does |
|-------|---------|--------------|
| **`swm-watchd`** | console_script → `swm/daemon.py:main` | Long-running. Loops over configured PRs at cadence; for each, runs `swm poll <repo>` (with CHG-1107's SWM-1100 §3 short-circuit). When stdout starts with `no change:`, continues silently — no adapter call. Otherwise, builds an `AgentEvidence` (CHG-1111 schema) from the new `PollRecord` + the prior `state_key` and calls `adapter.decide(...)`. On a `notify`-kind decision, appends to `notifications.jsonl` (CHG-1112 surface). On a `record-only` decision, appends to `inbox.jsonl`. On `ignore` or adapter `None` (timeout / malformed output): audit-only via CHG-1111's `agent-decisions.jsonl`. |
| **`swm inbox` CLI family** | `swm/cli.py` | `swm inbox` (list pending), `swm inbox view <id>`, `swm inbox approve <id> [--reason]`, `swm inbox deny <id> [--reason]`, `swm inbox snooze <id> --until 1h`. Joins `inbox.jsonl` (events from daemon) with `inbox-decisions.jsonl` (your y/n records) to compute pending events. `approve` reuses the existing `swm approve` SWM-1103 gate stack — no new authorization surface. |
| **Daemon config** | `~/.config/swm/watchd.toml` | Lists watched PRs + per-PR scope (read-only / standing-auth / merge-allowed) + `[agent]` block per CHG-1111 (`adapter`, `timeout_s`, `prompt_template`) + cadence. Authoritative source of who's being monitored. |

State layout adds two new JSONL files alongside the existing ones (CHG-1111's `agent-decisions.jsonl` is owned by that CHG):

```
state/<owner>/<repo>/pr-<N>/
  polls.jsonl            (existing)
  ledger.jsonl           (existing)
  box-misses.jsonl       (existing)
  agent-decisions.jsonl  (NEW — owned by CHG-1111; one row per adapter call)
  inbox.jsonl            (NEW — daemon writes maintainer-facing events; append-only)
  inbox-decisions.jsonl  (NEW — `swm inbox approve|deny|snooze` writes; append-only)
```

Adapter selection happens in `~/.config/swm/watchd.toml` `[agent]` section per CHG-1111 (default `adapter = "openclaw"`, `timeout_s = 30`). Cost-tier choice is the adapter's concern, not the daemon's — model IDs (e.g. DeepSeek Reasoner, Sonnet 4.5, Opus 4.7) live in the adapter's own config and the daemon does not name a model directly.

## Why

CHG-1107 closes the "no change" tax (90% of fires). This CHG closes the remaining "state changed" tax — even with CHG-1107 in place, every state change still wakes Claude Code (Opus by default), pulls full session context, and runs one Claude call per event. Switching to direct adapter calls (CHG-1111) on state changes only:

- ~5–10x cheaper per call vs Opus-via-Claude-Code (depends on adapter; OpenClaw+DeepSeek is the calibration baseline, but the cost story holds for any adapter that drops Claude-Code session preamble)
- ~10x cheaper context (no Claude Code session preamble; just the daemon's brief + the delta JSON)
- Combined with CHG-1107's "no change = no LLM": ~50x cheaper overall vs the current pattern
- Sub-minute polling becomes affordable, catching state changes faster

The interaction model (your B vote): events flow into `inbox.jsonl`; you run `swm inbox` periodically (or wire it into your terminal status line) to see pending decisions; `swm inbox approve <id>` fires the action under the same SWM-1103 gates the interactive `swm approve` already enforces. No Slack, no email, no third-party setup.

## Out of scope

- Multi-user inbox (one daemon, one maintainer). Multi-user is a Stage-3 concern.
- Webhook ingestion (the daemon polls; it does not subscribe to GitHub events). Polling is good enough at sub-minute cadence and avoids the auth/secret surface of webhooks.
- Auto-firing approve without inbox confirmation. Standing-auth grants from interactive sessions still require an explicit `swm inbox approve <id>` before the daemon fires anything Stage-3+.
- Web UI / GUI. CLI-only by design (per your option B vote).

## Compatibility

- Existing `swm` subcommands unchanged. `swm-watchd` is additive.
- `state/` layout is additive — pre-CHG repos work unchanged; new `inbox*.jsonl` files appear only after the first daemon run.
- No required new Python dep at the daemon layer. The OpenClaw adapter (CHG-1111 default) is a subprocess to an existing local CLI and needs no Python package. Users opting into the Anthropic adapter install via `[project.optional-dependencies] daemon-anthropic` per CHG-1111.
- The existing Claude-Code-cron monitoring path keeps working — daemon is opt-in; you can run both simultaneously during a transition window.

## Risks (each backed by a test)

| Risk | Mitigation | Test |
|------|-----------|------|
| Daemon crashes silently and you stop getting events | Daemon writes a heartbeat to `state/_daemon_heartbeat.json` every loop; `swm inbox` warns "daemon last seen N min ago" if heartbeat is stale | `test_inbox_warns_when_heartbeat_stale` |
| Adapter credential in plain config file | Credential handling is owned by the adapter (CHG-1111). OpenClaw uses the existing local `openclaw` CLI's credential store; the optional Anthropic adapter reads `ANTHROPIC_API_KEY` env or `~/.secrets/anthropic_api_key` (mode 600/400) per CHG-1111. Daemon never touches credentials directly. | `test_daemon_refuses_to_start_when_adapter_unhealthy` |
| Adapter hallucinates a state change and writes a spurious inbox event | Adapter output is a Pydantic-validated `AgentDecision` (CHG-1111 schema) before any side effect; `evidence_refs` must be a subset of refs the daemon supplied; the actual `swm approve` call still runs through the SWM-1103 gate stack on `swm inbox approve`. The adapter cannot bypass the gates. | `test_inbox_approve_invokes_swm_approve_with_full_gates` |
| Standing-auth grants leak across PRs because the daemon "remembers" | Daemon is stateless on standing-auth — every event requires a fresh `swm inbox approve` from the maintainer. Standing-auth in interactive Claude sessions does NOT cross over to the daemon | `test_daemon_does_not_apply_session_standing_auth` |
| `inbox.jsonl` grows forever | `swm inbox prune --before 30d` (manual). Daemon does not auto-prune (audit trail bias) | `test_inbox_prune_filters_by_age` |
| Two `swm inbox approve` runs race on the same event | Decision file write is atomic per JSONL line; double-approve produces two decision entries — `swm inbox view` shows the first as winning, second as no-op. Acceptable; not race-mitigated beyond that | `test_inbox_double_approve_is_idempotent_at_action_layer` |

## Acceptance

- [ ] `swm-watchd start --config ~/.config/swm/watchd.toml` runs and writes a heartbeat every loop.
- [ ] On a real state change in the configured PR, an event lands in `inbox.jsonl` within ≤ 2 cadence cycles.
- [ ] `swm inbox` lists exactly the pending events (events with no terminal decision); shows daemon heartbeat freshness.
- [ ] `swm inbox approve <id>` reuses `swm approve` and produces a `submit_review_approve` ledger entry (existing audit trail).
- [ ] `swm inbox deny <id>` records a decision but does NOT call `swm approve`.
- [ ] `swm inbox snooze <id> --until 1h` hides the event from `swm inbox` for the duration; reappears after.
- [ ] On a 24-hour test window with one busy PR, total adapter call cost (whichever provider the deployed adapter targets) is < 10% of the equivalent cron-Opus baseline.
- [ ] `pytest` ≥ 80% gate; new modules ≥ 90% line coverage.
- [ ] `af validate --root .` clean.
- [ ] Trinity fast-review (≥ 2 providers ≥ 9.0 mean) approves.

## Approval

- [ ] Approved by: <reviewer> on <date>

---

## Change History

| Date | Change | By |
|------|--------|----|
| 2026-05-08 | Initial proposal — replace cron+Opus monitoring with a Sonnet-driven local daemon + `swm inbox` CLI for maintainer authorization. Depends on CHG-1107 (SWM-1100 §3 short-circuit in `swm poll`) for the routine-polling cheap path. | Claude Code |
| 2026-05-09 | Targets line updated to delegate the LLM call to the adapter from CHG-1111 (provider-neutral; OpenClaw first impl) instead of the hardcoded `swm/sdk_client.py` Anthropic-SDK wrapper. `anthropic` removed from required deps; available via `[project.optional-dependencies] daemon-anthropic` for users who pick that adapter. Required by CHG-1111's acceptance criterion that this PR also amends SWM-1108 (Codex review #3212762800 on PR #10). | Claude Code |

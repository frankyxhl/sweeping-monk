# CHG-1107: `swm watch --quiet` — exit-code-driven cron path

**Applies to:** SWM project
**Last updated:** 2026-05-08
**Last reviewed:** 2026-05-08
**Status:** Proposed
**Date:** 2026-05-08
**Requested by:** Frank Xu (interactive session, after observing ~15 "no change" cron fires on PR #1 each consuming a full Opus round-trip for one line of output)
**Priority:** Medium
**Change Type:** Normal
**Targets:** New `swm/cli.py` subcommand `watch` (~80 LoC). No edits to `state.py`, `models.py`, `guarded.py`. No changes to existing commands.

---

## What

A new read-only CLI subcommand that performs the same `state_key` comparison as `swm poll` but exits silently when nothing changed. Cron prompts can short-circuit the LLM round-trip when there's nothing to surface.

```bash
swm watch <owner/repo> <pr> [--state-dir DIR]
```

Behavior:
- **No change** since last poll: `exit 0`, **no stdout output** at all.
- **State changed**: `exit 1`, single JSON line on stdout naming the changed dimensions:
  ```json
  {"repo":"frankyxhl/trinity","pr":68,"head_sha":"8badd1be","prior_head_sha":"afde482f","status":"ready","prior_status":"pending","changed":["head","status","codex_signal"],"summary":"head bump + status flip pending→ready + codex 👀→👍"}
  ```
- **Error** (gh failure, repo not configured): `exit 2`, error to stderr.

Cron prompts become:

```bash
out=$(cd /Users/frank/Projects/sweeping-monk && source .venv/bin/activate && swm watch frankyxhl/trinity 68 --state-dir /Users/frank/Projects/sweeping-monk/state 2>&1)
[ -z "$out" ] && exit 0  # no LLM wake-up
echo "$out"  # state changed — Claude reads the JSON and surfaces it
```

## Why

The current cron prompts perform `swm poll` then ask Claude to compare against prior state and emit "no change" or a state-change report. ~95% of fires are "no change". Each one burns a full Opus round-trip for a single line that's deterministically computable from `state_key`. Pushing the comparison into Python makes routine polling free.

`swm watch` is a thin wrapper over `swm poll` + the existing `state_key()` method on `PollRecord` — the comparison logic already exists; we just expose it as an exit-code-driven entry point.

## Out of scope

- Long-running daemon mode. That's CHG-1108.
- Stage 1.5 thread sync. `swm poll --sync` still owns that — `swm watch` calls `swm poll --sync` internally so sync semantics are preserved.
- New JSONL files. Reuses existing `polls.jsonl`.

## Compatibility

- Existing cron prompts continue to work unchanged. `swm watch` is purely additive.
- Existing `swm poll` behavior is unchanged.

## Risks (each backed by a test)

| Risk | Mitigation | Test |
|------|-----------|------|
| `swm watch` swallows real errors as "no change" | Distinguish exit codes: `0`=quiet success, `1`=changed, `2`=error. Errors go to stderr, never stdout. | `test_watch_exits_2_on_gh_failure` |
| First-ever poll for a PR (no prior `state_key`) is mishandled | Treat first poll as a state change (always emit `exit 1` + JSON with `prior_*=null`). | `test_watch_first_poll_emits_change` |
| JSON output drifts and breaks cron consumers | Use `pydantic` model `WatchDelta` for output; tests pin the field set. | `test_watch_delta_json_schema_stable` |
| State stored on disk diverges from Python's in-memory snapshot, producing spurious "no change" | The watch subcommand does a full `swm poll` first (refreshes `polls.jsonl`), then compares the new latest poll against the previous-to-last poll, not against in-memory. | `test_watch_compares_against_previous_jsonl_entry` |

## Acceptance

- [ ] `swm watch <repo> <pr>` on a fresh state dir exits 1 with JSON (first poll = change).
- [ ] Run twice in a row against an idle PR: second invocation exits 0 with empty stdout.
- [ ] When CI flips green between runs: exits 1 with `"changed": ["ci"]`.
- [ ] When head SHA bumps: exits 1 with `"changed": ["head", "ci"]` (CI typically resets on new head).
- [ ] `pytest` ≥ 80% gate passes; new code ≥ 90% line coverage.
- [ ] `af validate --root .` clean.
- [ ] One real cron prompt updated to use the exit-code pattern; observe ≥ 90% reduction in LLM wake-ups across a 24-hour window.
- [ ] Trinity fast-review (≥ 2 providers ≥ 9.0 mean) approves.

## Approval

- [ ] Approved by: <reviewer> on <date>

---

## Change History

| Date | Change | By |
|------|--------|----|
| 2026-05-08 | Initial proposal — push state-key comparison into Python so "no change" cron fires don't wake the LLM. Triggered by ~15 "no change" reports on PR #1 in one session, each costing an Opus round-trip. | Claude Code |

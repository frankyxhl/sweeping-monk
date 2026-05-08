# CHG-1107: Implement SWM-1100 §3 short-circuit in `swm poll`

**Applies to:** SWM project
**Last updated:** 2026-05-08
**Last reviewed:** 2026-05-08
**Status:** Proposed
**Date:** 2026-05-08
**Requested by:** Frank Xu (interactive session, after observing ~15 "no change" cron fires on PR #1 each consuming a full Opus round-trip for one line of output)
**Priority:** Medium
**Change Type:** Normal
**Targets:** `swm/poll.py` — add the unimplemented SWM-1100 §3 short-circuit (~10 LoC). No new subcommand, no new model, no new exit code, no new JSON schema.

---

## What

SWM-1100 §3 already specifies the desired behavior:

> If `state_key` matches the cached value from the previous poll for this PR, emit a single line and skip to step 7: `no change: PR #N still <status> @ <short_sha>`

That's never been implemented in `swm/poll.py`. `PollRecord.state_key()` exists at `swm/models.py:99`; `dashboard.py:242` uses it for the history view's run-collapsing; `poll.py` itself has zero references to it.

This CHG is the implementation patch:

- After computing the new `PollRecord` for a PR (and BEFORE writing it to `polls.jsonl`), read `store.latest_poll(repo, pr)` to get the prior poll.
- If `prior.state_key() == new.state_key()`: append the new record (audit trail unbroken) and emit one line: `no change: <repo>#<pr> still <status> @ <short_sha>`.
- Else: append + render the full dashboard card as today.

Cron prompt update (per-prompt, ~1 line):

```bash
out=$(cd /Users/frank/Projects/sweeping-monk && source .venv/bin/activate && swm poll <repo> --sync 2>&1)
echo "$out" | grep -q "^no change:" && exit 0   # silent quiet path; LLM never wakes
echo "$out"   # state changed — Claude reads the dashboard card and surfaces it
```

## Why

90%+ of cron fires are "no change". Each one currently emits a full dashboard card and wakes the LLM for one round-trip producing one summary line. The SOP says this should short-circuit; the code never made it happen. Implement the SOP.

## Out of scope

- Anthropic SDK / daemon / `swm inbox`. That's CHG-1108.
- Extending `state_key` to include `codex_pr_body_signal` (👀 vs 👍 transitions are caught indirectly via `codex_open` count + `status` flips). If field-granularity diff turns out to matter, it's a separate small CHG.
- Changing exit codes. `swm poll` keeps emitting `0` on success regardless of "no change vs changed" — the cron prompt distinguishes via the textual `^no change:` prefix.
- Dropping append on match (always append; the SOP wants the full audit trail).

## Compatibility

- `swm poll` exit codes unchanged.
- `polls.jsonl` schema unchanged; same record shape, just emitted conditionally to stdout.
- Existing cron prompts still work without modification — they just produce the same redundant "no change" reports as before. The token savings come once each cron prompt is updated to the `grep -q ^no change` pattern.
- `swm dashboard / history / summary / approve / tick / ledger / rule-coverage` unaffected.

## Risks (each backed by a test)

| Risk | Mitigation | Test |
|------|-----------|------|
| Short-circuit fires when first poll for a PR (no prior record) | First poll = no prior → render full card; only fire short-circuit when `latest_poll(...)` returns a record AND state_keys match | `test_poll_renders_full_card_on_first_observation` |
| Short-circuit suppresses a real state change because `state_key` is too coarse | `state_key` covers `(pr, head_sha, ci_tuple, codex_open, status)` — every dimension the dashboard surfaces. Codex 👀↔👍 reactions move `status` (PENDING↔READY) so they're caught. New finding moves `codex_open`. Head bump moves `head_sha`. CI flip moves `ci_tuple`. | `test_poll_renders_full_card_on_each_state_key_dimension_change` (parametrized over the 4 dimensions) |
| `polls.jsonl` audit trail breaks if "no change" polls aren't appended | Always append; only the render is conditional | `test_poll_appends_new_record_even_when_state_unchanged` |

## Acceptance

- [ ] First-ever `swm poll <repo>` for a PR renders the full dashboard card (no prior to compare against).
- [ ] Second `swm poll <repo>` against an idle PR emits only `no change: <repo>#<pr> still <status> @ <short_sha>` on stdout.
- [ ] When the head SHA bumps between runs: full dashboard card on next run.
- [ ] When CI flips between runs: full dashboard card.
- [ ] When a new Codex finding lands: full dashboard card (codex_open changes).
- [ ] `polls.jsonl` line count grows by exactly 1 per `swm poll` invocation regardless of whether the render was full or short-circuited.
- [ ] One real cron prompt updated with the `grep -q "^no change:" && exit 0` pattern; observe that cron fires emitting only "no change" do not produce LLM round-trips on the next 24h's events.
- [ ] `pytest` ≥ 80% gate; new code ≥ 90% line coverage.
- [ ] `af validate --root .` clean.
- [ ] Trinity fast-review (≥ 2 providers ≥ 9.0 mean) approves the patch + this rewritten CHG.

## Approval

- [ ] Approved by: <reviewer> on <date>

---

## Change History

| Date | Change | By |
|------|--------|----|
| 2026-05-08 | Initial proposal — new `swm watch --quiet` subcommand. | Claude Code |
| 2026-05-08 | Rewrite per trinity round-1 review (GLM 7.88 / DeepSeek 6.65, both FAIL): convergent findings flagged JSON-vs-state_key mismatch, WatchDelta contradiction, and exit-code collision. DeepSeek's necessity-blocker (#5): SWM-1100 §3 already specifies this behavior — the right patch is implementing the SOP, not adding a new subcommand. CHG reduced from ~80 LoC + new subcommand to ~10 LoC patch on `swm poll`. | Claude Code |

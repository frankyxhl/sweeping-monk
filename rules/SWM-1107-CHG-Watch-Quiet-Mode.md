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

A ~10-LoC patch on `swm/poll.py`: after computing the new `PollRecord` for a PR (and BEFORE writing it to `polls.jsonl`), read `store.latest_poll(repo, pr)` to get the prior poll. If `prior.state_key() == new.state_key()`: append the new record (audit trail unbroken) and emit one line: `no change: <repo>#<pr> still <status> @ <short_sha> · codex_open=<k>`. Else: append + render the full dashboard card as today.

Cron prompt update (one line per prompt):

```bash
out=$(cd /Users/frank/Projects/sweeping-monk && source .venv/bin/activate && swm poll <repo> --sync 2>&1)
echo "$out" | grep -q "^no change:" && exit 0   # silent quiet path; LLM never wakes
echo "$out"   # state changed — Claude reads the dashboard card and surfaces it
```

The emit format extends SWM-1100 §3's `no change: PR #N still <status> @ <short_sha>` with two refinements: (a) `<repo>#<pr>` prefix instead of `PR #N` so cron grep is unambiguous when watching multiple repos; (b) `· codex_open=<k>` suffix preserved verbatim from the SOP so a glance distinguishes "still 0 open findings" from "still 3 open findings". This CHG also commits to updating SWM-1100 §3's emit example to match in the same patch.

## Why

In one observation session, ~15 cron fires for PR #1 each emitted the same "no change" line — one full Opus round-trip per single-line output, ~95% of fires were this shape. Pushing the comparison into Python makes routine polls free at the LLM layer.

(SWM-1100 §3 already specified this short-circuit, but the line was aspirational — `swm/poll.py` has zero references to `state_key`, verified by grep. So this CHG is both an evidence-driven optimization AND the implementation of an existing-but-unimplemented SOP step.)

## Out of scope

- Anthropic SDK / daemon / `swm inbox`. That's CHG-1108.
- Extending `state_key` to include `codex_pr_body_signal` (👀 vs 👍 transitions move `status` PENDING↔READY, so they're caught via the existing `status` element). If field-granularity diff turns out to matter, it's a separate small CHG.
- Changing exit codes. `swm poll` keeps emitting `0` on success regardless of "no change vs changed" — the cron prompt distinguishes via the textual `^no change:` prefix.
- Dropping append on match (always append; the SOP wants the full audit trail).
- Aligning `dashboard.py:242`'s coarser collapse-key `(status, head_sha, codex_open)` with `models.state_key()`'s 5-element tuple. The dashboard's coarser key is intentional for visual run-collapsing in the history view — it deliberately ignores `pr` (already grouped) and `ci_tuple` (would over-fragment the visual). This CHG keeps that asymmetry; if a future divergence bites us, it's a separate fix.

## Compatibility

- `swm poll` exit codes unchanged.
- `polls.jsonl` schema unchanged; same record shape, just emitted conditionally to stdout.
- Existing cron prompts still work without modification — they just produce the same redundant "no change" reports as before. The token savings come once each cron prompt is updated to the `grep -q ^no change` pattern.
- `swm dashboard / history / summary / approve / tick / ledger / rule-coverage` unaffected.
- `state_key()` returns 5 elements `(pr, head_sha, ci_tuple, codex_open, status)` per `swm/models.py:99`. SWM-1100 §3's prose lists 4 — that prose is stale (predates the model definition). This CHG includes a one-line update to SWM-1100 §3 to match the actual 5-tuple, alongside the `swm/poll.py` patch.

## Risks (each backed by a test)

| Risk | Mitigation | Test |
|------|-----------|------|
| Short-circuit fires when first poll for a PR (no prior record) | First poll = no prior → render full card; only fire short-circuit when `latest_poll(...)` returns a record AND state_keys match | `test_poll_renders_full_card_on_first_observation` |
| Short-circuit suppresses a real state change because `state_key` is too coarse | `state_key` covers `(pr, head_sha, ci_tuple, codex_open, status)` — every dimension the dashboard surfaces. `pr` is identity (won't change within one PR's poll). The 4 mutating dimensions: `head_sha` (head bump), `ci_tuple` (CI flip), `codex_open` (new finding), `status` (PENDING↔READY catches Codex 👀↔👍 reactions). | `test_poll_renders_full_card_on_each_state_key_dimension_change` (parametrized over the 4 mutating dimensions) |
| `polls.jsonl` audit trail breaks if "no change" polls aren't appended | Always append; only the render is conditional | `test_poll_appends_new_record_even_when_state_unchanged` |

## Acceptance

- [ ] First-ever `swm poll <repo>` for a PR renders the full dashboard card (no prior to compare against).
- [ ] Second `swm poll <repo>` against an idle PR emits only `no change: <repo>#<pr> still <status> @ <short_sha>` on stdout.
- [ ] When the head SHA bumps between runs: full dashboard card on next run.
- [ ] When CI flips between runs: full dashboard card.
- [ ] When a new Codex finding lands: full dashboard card (codex_open changes).
- [ ] `polls.jsonl` line count grows by exactly 1 per `swm poll` invocation regardless of whether the render was full or short-circuited.
- [ ] **Implementer-check** (merge gate): a deterministic subprocess test asserts that `swm poll` against an idle PR emits stdout starting with `^no change:` and that `grep -q "^no change:"` exits 0. Pinned in `tests/test_cli.py`.
- [ ] **Field observation** (post-merge, not a merge gate): one real cron prompt updated with the `grep -q "^no change:" && exit 0` pattern; over a 24-hour window count LLM session invocations triggered by that cron and confirm the count drops to ≈ the count of state-change events for that PR.
- [ ] SWM-1100 §3's emit-format example is updated in the same patch to match `<repo>#<pr>` + `· codex_open=<k>` and the 5-element `state_key`.
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
| 2026-05-08 | Round-2 fixes per trinity (GLM 8.13 / DeepSeek 7.35, both FAIL but "ship with these fixes"): emit format aligned with SWM-1100 §3 (added `· codex_open=<k>` suffix); risk table dimension count corrected (4 mutating + `pr` identity = 5-tuple); §Why now leads with the 15-wasted-round-trips operational evidence (SOP citation demoted to confirmatory); 24h acceptance criterion split into deterministic implementer-check (merge gate) + field observation (post-merge); explicit out-of-scope note that `dashboard.py:242`'s coarser collapse-key is intentional and not aligned by this CHG; SWM-1100 §3 emit-example update committed to as part of the same patch. | Claude Code |

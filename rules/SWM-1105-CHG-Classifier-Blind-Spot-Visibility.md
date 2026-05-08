# CHG-1105: Classifier Blind-Spot Visibility (`swm rule-coverage` + `box-misses.jsonl`)

**Applies to:** SWM project
**Last updated:** 2026-05-08
**Last reviewed:** 2026-05-08
**Status:** Proposed
**Date:** 2026-05-08
**Requested by:** Frank Xu (interactive session, after the round-2 fix on `frankyxhl/trinity#67`)
**Priority:** Medium
**Change Type:** Normal
**Targets:** `swm/cli.py` (new `rule-coverage` subcommand), `swm/guarded.py` (skip-side-effect hook in `classify_box` callers), `swm/state.py` (new JSONL helper), `swm/models.py` (new `BoxMiss` model). Companion: `SWM-1106-SOP-Rule-Addition-Cycle.md` codifies the fix loop this CHG enables.

---

## What

When `swm tick` skips an unchecked box, the skip is currently silent. CHG-1105 makes it persistent and queryable.

| Addition | Behavior |
|----------|----------|
| **`state/<owner>/<repo>/pr-<N>/box-misses.jsonl`** | Append-only JSONL. Every `swm tick` invocation appends one line per skipped box: `{ts, repo, pr, head_sha, box_text, rule_id, satisfied, reason}`. Path-driven layout matches `polls.jsonl` / `ledger.jsonl`. |
| **`swm rule-coverage [--repo <owner/repo>] [--since 7d] [--threshold N]`** | Read-only command. Walks every `box-misses.jsonl` in scope, groups by canonical box text (lowercased, whitespace-collapsed), prints `count │ canonical text │ matched_rule (or "—") │ last_seen_at`. `--threshold` (default 3) hides rows with `count < N` to keep the output actionable. |

The fix loop that converts misses into rules is the separate `SWM-1106` SOP.

## Why

Round-2 of CHG-1104 surfaced a paths-ignore blind spot on `trinity#67` only because the maintainer noticed and pushed back; the watchdog had no data-driven way to flag the recurring pattern. CHG-1105 makes future blind spots visible without that pushback. The classifier stays conservative — false ticks corrupt the ledger, false skips are friction (asymmetric cost) — but observability is now first-class.

This is `git status` for the classifier: not a new gate, a window onto the existing one.

## Out of scope

- LLM-assisted classification (breaks ledger reproducibility — "would the watchdog tick this again next week?" must be answerable from JSONL alone).
- Per-repo `box-rules.yaml` config files (rules belong in `BOX_RULES` where reviewers see them).
- Auto-generating regex from miss text (maintainer reviews each suggestion; the `rule-coverage` command surfaces, never writes).
- A `suggested_rule_id` column in the `rule-coverage` output. Defer until miss data exists to derive a heuristic from.

## Compatibility

- `box-misses.jsonl` is a new file. Repos that have never run `swm tick` after this CHG land have no file; `rule-coverage` reports "no misses recorded" and exits 0.
- `tick` command's existing output (rich table + ledger entry) is unchanged; the miss-write is a side effect, not a UX change.
- `LedgerEntry` model untouched. Misses live in their own file because they're observations, not authorized actions.
- **Canonicalization is intentionally minimal** — lowercased + whitespace-collapsed only. `"CI ubuntu passes"` and `"CI ubuntu-latest passes"` will appear as **separate rows** because their canonical forms differ. That's a feature: the maintainer sees both rows and decides whether the rule should cover both, and SWM-1106's regex-design step is where that judgement happens. Smarter canonicalization (token sets, edit distance) would obscure the choice and is deferred until a real collision case appears.

## Risks (each backed by a test)

| Risk | Mitigation | Test |
|------|-----------|------|
| Miss writes silently fail and skips become permanently invisible | Wrap append in the same path-driven helper as `append_ledger`; failure raises `OSError` and surfaces in `tick` stderr | `test_box_miss_append_round_trip` |
| Canonicalization is too aggressive, distinct claims grouped together | Lowercase + collapse-whitespace only; do not strip punctuation, parentheticals, or backticks | `test_rule_coverage_groups_by_canonical_text_only` |
| Coverage report becomes noisy on a long-lived monorepo | `--since 7d` (default) + `--threshold 3` (default) bound the output | `test_rule_coverage_filters_by_since`, `test_rule_coverage_filters_by_threshold` |

## Acceptance

- [ ] `swm tick` on `frankyxhl/trinity#67` (replay against `FakeGhClient` with the body that originally produced 2 skipped CI boxes) writes 2 entries to `box-misses.jsonl`.
- [ ] `swm rule-coverage frankyxhl/trinity` shows the two CI rows with `matched_rule="ci.ubuntu" / "ci.macos"` (predicate-refused branch) — distinct from a synthetic `[ ] CHANGELOG updated` row which shows `matched_rule="—"` (coverage gap).
- [ ] `pytest` ≥ 80% coverage gate passes; new code ≥ 90% line coverage.
- [ ] `af validate --root /Users/frank/Projects/sweeping-monk` clean.
- [ ] Trinity fast-review (≥ 2 providers ≥ 9.0 mean) approves the post-fix CHG.

## Approval

- [ ] Approved by: <reviewer> on <date>

---

## Change History

| Date | Change | By |
|------|--------|----|
| 2026-05-08 | Initial proposal — make classifier blind spots visible across PRs via per-skip JSONL + a coverage report command. Triggered by `trinity#67` round-2 fix. | Claude Code |
| 2026-05-08 | Round-1 fixes per Trinity fast-review (GLM 8.13 / FAIL, DeepSeek 5.80 / FAIL): extracted embedded SOP to `SWM-1106-SOP-Rule-Addition-Cycle.md` (atomicity); dropped undefined `suggested_rule_id` column (YAGNI); dropped non-risk rows 3-4 from risk table; trimmed §Why from 3 paras to 1; fixed first-person register to maintainer-third-person; justified `--threshold 3` default; added explicit canonicalization compatibility note. | Claude Code |

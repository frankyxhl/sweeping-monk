# SOP-1106: Rule-Addition Cycle (closing classifier blind spots)

**Applies to:** SWM project
**Last updated:** 2026-05-08
**Last reviewed:** 2026-05-08
**Status:** Active
**Depends on:** CHG-1105 (`box-misses.jsonl` data + `swm rule-coverage` command surface the misses this SOP closes)

---

## What Is It?

The canonical workflow for converting a discovered classifier blind spot into a permanent fix in `swm/guarded.py`'s `BOX_RULES`. Triggered by `swm rule-coverage` output (or by a maintainer noticing a skipped box mid-session). Result: one regex + one predicate + one test, ~50 LoC, ~10 min wall-clock.

## Why

`swm tick`'s classifier is intentionally conservative — false ticks corrupt the ledger, false skips are mere friction. The cost of conservatism is recurring skips on patterns the classifier *could* have handled if it had a rule. Without an explicit cycle, those skips either pile up silently or get ad-hoc patched without tests. This SOP makes the fix loop the design.

---

## When to Use

- `swm rule-coverage` shows a row with `count >= 3` and `matched_rule = "—"` (genuine coverage gap).
- A maintainer manually notices `swm tick` skipped a box whose claim is verifiably true *now* on the current PR.
- A new repeatable PR-body shape appears (e.g., a CHANGELOG-updated box on every release PR).

## When NOT to Use

- Single-PR oddities (count=1) — wait for a third occurrence before adding a rule. One-offs don't justify table growth.
- Boxes whose claim cannot be *deterministically* checked from `PollRecord` data (e.g., "Manual smoke test on staging"). These are correctly unverifiable; the classifier's job is not to invent trust.
- Negation / inverted-meaning patterns ("CI is NOT required") — these need explicit handling rather than a one-line predicate; escalate to a CHG.

---

## Steps

### 1. Identify the canonical text

Run `swm rule-coverage <repo>` and pick a row with `count >= 3`. Copy the canonical text (lowercased, whitespace-collapsed) — that's the universe of box-text strings the new rule must cover.

### 2. Decide the predicate

What `PollRecord` field proves the claim?

| Claim shape | Predicate |
|-------------|-----------|
| "CI <runner> passes" | matching key in `poll.ci` is `SUCCESS` (or empty CI + `status == READY` for paths-ignore) |
| "Codex bot reviewed" | `poll.codex_pr_body_signal == "approved"` |
| "Coverage ≥ X%" | NOT YET — needs a coverage field on `PollRecord` first; defer |
| "CHANGELOG updated" | NOT YET — needs a `changed_files` field; defer |

If the predicate doesn't map to existing `PollRecord` data, defer the rule and file an extension proposal. Don't add fields just to satisfy one rule.

### 3. Write the regex

Constraint: the regex must match every observed canonical text in step 1, AND must NOT match texts whose meaning differs (e.g., "CI ubuntu-latest passes locally" should match the same rule, but "CI is gated by ubuntu-latest" should not).

Test the regex by eye against the worked examples *before* writing the code.

### 4. Add to BOX_RULES + write test

Edit `swm/guarded.py`:

```python
BOX_RULES: list[tuple[str, re.Pattern[str], Predicate]] = [
    ...
    ("<rule_id>", re.compile(r"<your regex>", re.IGNORECASE), <predicate_fn>),
]
```

Order matters — more specific patterns come before more general ones (`re.search` short-circuits at the first match in `classify_box`).

Add a test in `tests/test_guarded.py` covering both the satisfied and not-satisfied branches against `ready_poll` / `pending_poll` fixtures.

### 5. Replay against the original data

Run `swm tick <repo> <pr> --reason "verifying rule <rule_id>"` against the PR that originally surfaced the miss. The previously-skipped box should now flip; record the diff in the commit message.

If `swm rule-coverage` is implemented, re-run it and confirm the row is gone (count drops by however many polls had the miss).

### 6. Commit

Single commit per rule. Message format:

```
fix(swm): tick classifier — <claim shape> rule (<rule_id>)

<one paragraph: what gap, what predicate, what evidence triggered the fix>

Surfaced live: <repo>#<pr> (<title>) — <one-line context>
Suite: <N> passed (+<delta>), <coverage>% line coverage.
```

Push. No CHG required — rule additions are bounded, mechanical, and covered by this SOP.

---

## Worked Example: PR-#67 Round-2 Patch (commit `6df4d86`)

**Trigger:** `swm tick frankyxhl/trinity 67` skipped two boxes ("CI ubuntu-latest passes" and "CI macos-latest passes") on a docs-only PR. The maintainer pushed back; the gap was real.

**Predicate gap:** `_ci_runner_predicate` required `poll.ci` to contain a matching runner. Paths-ignore makes `poll.ci == {}`, so the predicate fell through to "not satisfied" even though the parent verdict (`status == READY`) had already trusted the empty-CI state.

**Fix:** trust transfer — when `poll.ci == {}` AND `poll.status == Status.READY`, return satisfied. When `poll.ci == {}` but status is anything other than READY (e.g., still in the 5-min CI grace window), keep refusing.

**Test:** three branches covered: empty-CI + ready (✓), empty-CI + pending (✗), all-CI rule with same trust transfer (✓).

**Total:** ~57 LoC, ~10 min wall-clock from "user pushback" to "fixed and pushed."

---

## Guard Rails

- Never add a rule without a test for both branches (satisfied + not-satisfied). Untested rules are worse than no rule — they create silent confidence.
- Never relax the conservative-classifier principle: if a predicate's data isn't strong enough to prove the claim, refuse. The `PollRecord` is the single source of truth.
- Never canonicalize across rule_ids (one rule per claim shape; don't fold "ci.ubuntu" into "ci.macos" because they share a runner pattern).
- Rule additions don't need a CHG. SOP-driven, capped at 1 regex + 1 predicate + 1 test per commit. Anything bigger (new `PollRecord` field, new claim category, new canonicalization scheme) is a CHG.

---

## Change History

| Date | Change | By |
|------|--------|----|
| 2026-05-08 | Initial version — extracted from CHG-1105 §"Rule-addition workflow" per trinity fast-review (GLM finding #2). PR-#67's round-2 patch is the worked example. | Claude Code |

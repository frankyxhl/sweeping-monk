# CHG-1104: Guarded Writes Automation (`swm approve`, `swm tick`, `swm ledger`)

**Applies to:** SWM project
**Last updated:** 2026-05-08
**Last reviewed:** 2026-05-08
**Status:** Proposed
**Date:** 2026-05-08
**Requested by:** Frank Xu (interactive session, after the first real Stage-3 unlock on `frankyxhl/trinity#66`)
**Priority:** Medium
**Change Type:** Normal
**Targets:** New `swm/guarded.py`; extends `swm/cli.py`, `swm/gh.py`, `swm/models.py`, `swm/state.py`

---

## What

Codifies SWM-1103 §§1–7 as three typer subcommands. Default permission stays Stage 1.5; the commands refuse to fire unless every SWM-1103 guard passes.

| Command | SWM-1103 step it codifies |
|---------|---------------------------|
| `swm approve <repo> <pr> --reason "<phrase>"` | §1 echo, §2 identity, §3 verdict gate (status=ready), §4 head freshness (incl. TOCTOU re-check), §5 execute, §6 verify, §7 ledger |
| `swm tick <repo> <pr> --reason "<phrase>"` | §2 identity (non-author), §3 verdict freshness, §5 single `gh pr edit --body-file`, §6 post-edit body diff verify, §7 ledger; per-box rule classifier (CI runner / Codex bot / all-CI-green) |
| `swm ledger <repo> <pr>` | Read-only complement |

`--yes` skips the interactive `[y/N]` prompt; `--reason` is required (no default), and lands in the ledger.

## Why

Manual SWM-1103 (the four maintainer-authorized writes performed today on `frankyxhl/trinity#66`) is error-prone: every step is a place future invocations can drift. Codifying the writes through `swm` makes the per-PR `ledger.jsonl` the system of record (not an "agent remembered to write it" side effect), and pins the box classifier to the same `PollRecord` the dashboard reads — so the dashboard and `tick` can never disagree.

## Out of scope

Stage-4 merge (`swm merge`), Stage-2 comment posting, label/title edits, and auto-detection of new box patterns beyond CI-runner / Codex-bot / all-CI-green. New patterns are 5-line additions to `BOX_RULES` when a repeatable PR-body shape appears.

## Compatibility

The hand-written ledger entries already in `state/frankyxhl/trinity/pr-66/ledger.jsonl` parse cleanly under the new `LedgerEntry` model via `extra="allow"`. No migration.

## Risks (each backed by a test)

| Risk | Mitigation | Test |
|------|-----------|------|
| Head SHA drifts during the `[y/N]` window | Re-fetch `headRefOid` between confirm and review submit; abort on diff | `test_approve_refuses_on_head_sha_drift_during_confirmation` |
| Box classifier auto-flips a textually-similar box that's actually false | Each rule consults `PollRecord` — text match alone never counts | `test_classify_box_*` (rule × poll matrix) |
| Ledger gets written when GitHub call failed | Ledger append happens *after* the gh call returns success AND post-call verify passes | `test_approve_does_not_ledger_when_review_call_fails*`, `test_tick_does_not_ledger_when_body_diff_mismatches` |
| Self-author bypass on tick | `tick_cmd` reads `identity.blocker` (same path as approve) | `test_tick_refuses_on_self_action` |
| `--yes` makes Stage-3 cheap | `--reason` is required (no default) on both `approve` and `tick`; the reason is logged | covered by required-arg typer behavior |
| `gh auth status` parsing breaks across CLI versions | Fail-closed: parser raises `GhCommandError`, command refuses with a clear message | `test_auth_active_login_raises_when_unparseable` |

## Acceptance

- [ ] `swm approve frankyxhl/trinity 66 --reason "..." --yes` reproduces the PR-#66 approval flow against `FakeGhClient`; ledger schema matches the manually-written entry.
- [ ] `swm tick frankyxhl/trinity 66 --reason "..." --yes` flips exactly the four boxes the maintainer flipped on PR #66; an unverifiable box stays unticked.
- [ ] `swm ledger` renders both legacy and new entries together.
- [ ] `pytest` ≥ 80% gate passes; new modules ≥ 90% line coverage.
- [ ] `af validate --root /Users/frank/Projects/sweeping-monk` clean.
- [ ] Trinity fast-review (≥ 3 providers ≥ 9.0 mean) approves the post-fix diff.

## Approval

- [ ] Approved by: <reviewer> on <date>

---

## Change History

| Date | Change | By |
|------|--------|----|
| 2026-05-08 | Initial proposal — codify SWM-1103 manual flow as `swm approve` / `tick` / `ledger` | Claude Code |
| 2026-05-08 | Round-1 fixes per Gemini + DeepSeek review: `tick` honors `identity.blocker`, `submit_review_approve` switches to `--body-file`, `tick`'s `--reason` is required, TOCTOU re-check inserted, four named-but-missing tests added; CHG compressed from 107 → ~60 lines | Claude Code |

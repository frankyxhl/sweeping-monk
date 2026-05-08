# CHG-1104: Guarded Writes Automation (`swm approve`, `swm tick`, `swm ledger`)

**Applies to:** SWM project
**Last updated:** 2026-05-08
**Last reviewed:** 2026-05-08
**Status:** Proposed
**Date:** 2026-05-08
**Requested by:** Frank Xu (interactive session, after first real Stage-3 unlock on `frankyxhl/trinity#66`)
**Priority:** Medium
**Change Type:** Normal
**Targets:** New module `swm/guarded.py`; extends `swm/cli.py`, `swm/gh.py`, `swm/models.py`, `swm/state.py`

---

## What

Three new typer subcommands that codify SWM-1103 (Maintainer-Authorized One-Shot Writes) so the gates run in code instead of in the agent's head. Default permission stage stays at 1.5 — the new commands refuse to fire unless every SWM-1103 guard passes, and the agent invokes them only after explicit per-action authorization in the same turn.

| Command | What it does | SWM-1103 step it codifies |
|---------|--------------|---------------------------|
| `swm approve <repo> <pr> --reason "<phrase>"` | Submits an `APPROVE` review with a templated factual body (CI runs + Codex signal + head SHA). Refuses unless the latest poll for this PR has `status=ready` against the *current* head SHA, and identity is non-self per `gh auth status` vs PR author. Appends a ledger entry on success. | §1 echo, §2 identity, §3 verdict gate (approve), §4 head freshness, §5 execute, §6 verify, §7 ledger |
| `swm tick <repo> <pr>` | Fetches PR body, parses `- [ ]` lines, classifies each against the latest poll using a small rule table (CI-runner-passes, Codex bot review, all-CI-green). Auto-flips boxes whose claim is verifiably true *now*; leaves unverifiable boxes (manual smoke, design checks) untouched. Single `gh pr edit --body-file` call, post-call diff verification, ledger entry. | §3 verdict gate (tick — head freshness only, since boxes are factually checked per-line), §5–§7 |
| `swm ledger <repo> <pr>` | Read-only — prints the per-PR JSONL audit trail as a rich.Table. No gates needed. | (read-only complement) |

All three commands take an explicit `--yes` to skip the interactive `[y/N]` prompt; without it the planned action is shown first and the user has to confirm.

## Why

Three forces converge on the same conclusion:

1. **Manual SWM-1103 is error-prone.** The four maintainer-authorized writes performed today on `frankyxhl/trinity#66` (one approve + four checkbox flips) each required: parsing `gh auth status`, looking up the PR author, reading `state/.../polls.jsonl` for the latest verdict, comparing head SHAs, building a body file, diffing, and appending a structured ledger line. Each manual step is a place a future invocation can drift (pick the wrong account, edit a stale body, forget the ledger).
2. **The ledger needs to be the system of record.** Today an agent can mutate GitHub from any session and the only proof is the agent's chat output. Codifying the writes through `swm` means *every* one-shot Stage-3 write goes through a single function that always appends to `state/<repo>/pr-<N>/ledger.jsonl` — making the ledger an authoritative audit trail rather than a hopefully-remembered side effect.
3. **`swm tick` removes the most error-prone step.** Manually deciding "is this box now factually true?" is exactly the spot where ad-hoc judgement creates inconsistency. Pinning each verifiable rule to the same `PollRecord` the dashboard already shows means *the dashboard and the tick command can never disagree*. Unverifiable boxes (manual smoke tests, design judgements) explicitly do not match any rule, so they stay untouched.

## Scope

**In scope (this CHG):**
- Implement `swm approve`, `swm tick`, `swm ledger` commands.
- Add `LedgerEntry` model (pydantic, `extra="allow"` for forward compatibility with the manually-written entry already in `pr-66/ledger.jsonl`).
- Add `StateStore.append_ledger` / `read_ledger` (path: `state/<owner>/<repo>/pr-<N>/ledger.jsonl`).
- Add `GhClient.auth_active_login`, `submit_review_approve`, `edit_pr_body` (latter via `--body-file` + tempfile, never `--body` arg-expansion).
- Add `swm/guarded.py` containing pure-function guard logic: `check_identity`, `check_verdict_approve`, `check_verdict_tick`, `parse_unchecked_boxes`, `classify_box`, `apply_box_flips`, plus the rule table.
- Tests for every guard branch (happy path + refuse-on-self-approve + refuse-on-no-poll + refuse-on-stale-head-sha + refuse-on-status-not-ready + tick-leaves-unverifiable-boxes + ledger round-trip).
- BDD scenario: "maintainer authorizes approve and the watchdog refuses because head SHA moved".

**Out of scope (deferred):**
- Stage-4 merge command (`swm merge`). The SWM-1103 §When NOT to Use rule "merge requires a second explicit yes-merge turn" applies, but the second-turn confirmation is hard to model in CLI ergonomics; defer until the maintainer asks for it.
- Posting comments / requesting changes (Stage-2). Out of charter.
- Auto-detecting which boxes to flip beyond the seed rule set (CI-runner / Codex-bot-review / all-CI-green). New rules are 5-line additions to `BOX_RULES` and are added when a new repeatable PR-body shape appears.

## Impact

**Files added:**
- `swm/guarded.py` (~180 LoC)
- `tests/test_guarded.py` (~250 LoC)
- `rules/SWM-1104-CHG-Guarded-Writes-Automation.md` (this file)

**Files modified:**
- `swm/cli.py` — three new typer subcommands + helper for confirmation prompt
- `swm/gh.py` — three new methods: `auth_active_login`, `submit_review_approve`, `edit_pr_body`
- `swm/models.py` — `LedgerAction` enum + `LedgerEntry` model
- `swm/state.py` — `append_ledger`, `read_ledger`, `_ledger_path`
- `swm/__init__.py` — export new symbols
- `tests/conftest.py` — extend `FakeGhClient` with auth/review/edit stubs
- `tests/test_models.py`, `tests/test_state.py`, `tests/test_gh.py`, `tests/test_cli.py` — extend with new cases
- `tests/features/watchdog.feature` + step file — add the head-SHA-moved scenario
- `rules/SWM-0000-REF-Document-Index.md` — auto-regenerated by `af index`

**Charter / SOPs not changed:**
- `CLAUDE.md` Stage 1.5 / 1 / forbidden lists already point at SWM-1103. No new permissions are granted by this CHG; it's an implementation of the existing SOP.
- `SWM-1100`, `SWM-1101`, `SWM-1102`, `SWM-1103` unchanged.

## Compatibility

The manually-written ledger entries already in `state/frankyxhl/trinity/pr-66/ledger.jsonl` parse cleanly under the new `LedgerEntry` model (extra fields `boxes_flipped` / `diff_lines_changed` are accepted via `extra="allow"`). No migration needed.

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| `swm approve` fires while the head SHA moves between verdict-read and the actual `gh pr review` call (TOCTOU) | Re-fetch head SHA inside `gh pr view` immediately before the review call; if it differs from the verdict's head, refuse and exit non-zero. Test: `test_approve_refuses_on_head_sha_drift`. |
| Box classifier auto-flips a box with text similar to a verifiable claim but actually means something else (e.g. a new line "CI ubuntu-latest passes locally") | Each rule predicate must consult the `PollRecord` — text-only matches never count. Worst-case false positive: a box claims "X" but X *is* actually true per the poll; no harm done, the maintainer can untick manually. Test: `test_tick_only_flips_when_predicate_passes`. |
| Ledger writes succeed but the GitHub call failed (or vice versa) | Ledger append happens *after* the gh call returns success and after post-call verification; on any error the ledger is not written. A failed GitHub call leaves zero state. A failed verification (e.g. body diff mismatch) raises and exits non-zero. Test: `test_approve_does_not_ledger_when_review_call_fails`, `test_tick_does_not_ledger_when_body_diff_mismatches`. |
| The `--yes` flag makes Stage-3 actions feel cheap | `--yes` requires `--reason` to be set explicitly (no default). The reason ends up in the ledger. Same friction floor as the manual flow. |
| `gh auth status` parsing breaks across `gh` CLI versions | The parser is a small regex (`Logged in to .+ account (\S+)` near `Active account: true`). On parse failure, `auth_active_login` raises `GhCommandError` and `swm approve` refuses with a clear message — fail-closed. |

## Acceptance

- [ ] `swm approve frankyxhl/trinity 66 --reason "..." --yes` reproduces today's PR-#66 approval flow end-to-end against a `FakeGhClient`, and the resulting ledger entry matches the schema of the manually-written one.
- [ ] `swm tick frankyxhl/trinity 66 --yes` flips exactly the four boxes the maintainer flipped today; a fifth synthetic box ("Manual smoke test on staging") stays unticked.
- [ ] `swm ledger frankyxhl/trinity 66` renders both the manually-written entries and the new ones together as one rich.Table.
- [ ] `pytest` ≥ 80% coverage gate still passes; new modules ≥ 90% line coverage.
- [ ] `af validate --root /Users/frank/Projects/sweeping-monk` clean.
- [ ] Trinity fast-review (Codex + Gemini + GLM + DeepSeek single-pass) approves at ≥ 9.0 mean across providers.

## Approval

- [ ] Approved by: <reviewer> on <date>

---

## Change History

| Date | Change | By |
|------|--------|----|
| 2026-05-08 | Initial proposal — codify SWM-1103 manual flow as `swm approve` / `tick` / `ledger` | Claude Code |

# SOP-1103: Maintainer-Authorized One-Shot Writes

**Applies to:** SWM project
**Last updated:** 2026-05-08
**Last reviewed:** 2026-05-08
**Status:** Active
**Depends on:** CLAUDE.md (Stage permission model), SWM-1100 (poll cycle for verdict context)

---

## What Is It?

A controlled escape hatch for Stage-3+ GitHub write actions (submitting reviews, editing PR bodies, merging) that the maintainer authorizes ad-hoc inside an interactive session. The watchdog's default permission stays at Stage 1.5; this SOP defines how to safely execute a one-shot write without permanently widening the charter, and how to leave a durable audit trail.

## Why

Stage 1.5 only covers `resolveReviewThread` / `unresolveReviewThread`. In practice the maintainer occasionally asks the watchdog to cross that line — e.g., "approve this on my behalf", "tick the boxes that are now satisfied", "edit the title". Without an SOP these ad-hoc requests:

- Skip identity checks (the global rule mandates `ryosaeba1985` for agent-authored GitHub writes; self-approval, fork-only PAT scopes, and other edge cases need to be surfaced *before* the call).
- Leave no trace — the maintainer later can't tell which writes the watchdog made vs which they made themselves.
- Drift into "while I'm at it" scope creep (one approval becomes a body edit becomes a merge).

This SOP fixes all three by making each one-shot write narrowly scoped, identity-checked, and ledgered.

---

## When to Use

- Maintainer explicitly asks the watchdog to perform a Stage-3+ write inside the current session.
  - Examples: "approve PR #N on my behalf", "check the boxes on the PR body", "edit the PR title to …", "merge it for me".
- Authorization is bounded to a single PR and a single action (or a small explicit set named in the same turn).

## When NOT to Use

- Standing authorization across sessions ("from now on always approve when CI is green") — that is a charter change. Update CLAUDE.md / the Stage table instead, then merge it through the normal PR flow.
- Implicit authorization ("this PR looks good") — the maintainer must name the *action* (approve / edit / merge), not just signal satisfaction.
- Posting comments or reviews that contain new opinions (Stage 2 territory). Acceptable bodies for Stage-3 reviews are factual evidence summaries (CI status, Codex signal, head SHA) — not free-form commentary.
- Merging a PR. Treat this as Stage-4. Surface the request, confirm scope, but require a second explicit "yes, merge it" turn before calling `gh pr merge`.

---

## Steps

### 1. Restate the action and scope

Echo back what you heard in one sentence: action + target + identity. Examples:

- "Submitting an APPROVE review on `frankyxhl/trinity#66` as account X."
- "Editing the body of `frankyxhl/trinity#66` to flip 4 unchecked acceptance boxes."

Do not bundle unrelated actions. If the maintainer's turn names two actions (approve + check-boxes), file them as two separate ledger entries even when the call is on the same PR.

### 2. Identity check

Run `gh auth status`. Apply the rule:

| Account in keyring | PR author | Action allowed? |
|--------------------|-----------|-----------------|
| `ryosaeba1985` (the agent identity per global rule) | not the agent | Yes — preferred path. |
| `ryosaeba1985` | the agent | **GitHub blocks self-approval/self-merge.** Surface conflict; ask whether to approve as `frankyxhl` (the human/maintainer identity) or stand down. |
| Only `frankyxhl` | any | Surface to maintainer. The global rule prefers `ryosaeba1985`; getting an explicit OK to proceed as `frankyxhl` is required, and the deviation is logged in the ledger. |
| Neither | — | Stand down. Explain auth gap. Do not improvise a new token. |

### 3. Verdict-gate the write

Before any `gh pr review --approve` / `gh pr edit` / `gh pr merge`, the watchdog must already have a fresh local verdict that supports the action:

- `--approve` requires `status: ready` from the most recent SWM-1100 poll on the current head SHA.
- `gh pr edit` to flip an acceptance / test-plan checkbox requires that the box's claim is factually true *now* — verifiable from CI status, Codex signal, or a referenced artifact. Cosmetic body edits (typos, formatting) are out of scope; ask the maintainer to do them manually.
- Merge actions (Stage 4) require both the above plus the second-turn confirmation from §When NOT to Use.

If verdict ≠ ready, surface the conflict and stand down. The maintainer can override, but the override itself goes in the ledger.

### 4. Confirm the head SHA hasn't moved

```bash
gh pr view <N> --repo <owner>/<repo> --json headRefOid
```

Compare against the head SHA the verdict was computed on. If it changed, restart at SWM-1100 step 1 — never write against a stale verdict.

### 5. Execute the write

Single command. No batching across PRs. For body edits, prefer `--body-file` over `--body` so the new body is reviewable as a file before the call lands.

### 6. Verify result

Read back the changed surface to confirm the call landed:

- Approve → `gh pr view <N> --json reviewDecision,mergeStateStatus,latestReviews` should show the new `APPROVED` review.
- Body edit → re-fetch `body` and diff against the prepared file; the diff must be empty.

If verification fails, surface the error and do not retry without instruction.

### 7. Append a ledger entry

Write one JSONL line to `state/<owner>/<repo>/pr-<N>/ledger.jsonl` (append-only, never overwritten). Required fields:

```json
{
  "ts": "<ISO8601 UTC>",
  "repo": "<owner>/<repo>",
  "pr": <N>,
  "head_sha": "<full SHA the action targeted>",
  "action": "submit_review_approve | edit_pr_body_check_boxes | merge | …",
  "actor": "<gh login that performed the call>",
  "authorized_by": "maintainer (interactive session, one-shot Stage-<n> unlock)",
  "reason": "<one-line factual justification>",
  "evidence": { … verdict / CI / codex snapshot supporting the action … },
  "result": { … reviewDecision / mergeStateStatus / boxes_flipped … }
}
```

The ledger is the system of record for "did the watchdog do this?". Entries are never edited; corrections are added as new entries.

### 8. Report back to the maintainer

Compact verification table (pre/post) plus one line confirming the ledger entry was appended. Do not re-narrate the whole flow.

---

## Hard Constraints

- One action per authorization turn. Compound requests get split into multiple §1 echo-backs.
- Default permission state remains Stage 1.5 once the turn completes. The next session reverts to Stage 1.5 unless re-authorized.
- Never invent a new gh token, never `--no-verify`, never bypass branch protection.
- Never write a Stage-3+ action against a PR the watchdog has not locally polled in this session.
- Never approve a PR whose head SHA has changed since the verdict was computed.

---

## Change History

| Date | Change | By |
|------|--------|----|
| 2026-05-08 | Initial version — codify one-shot Stage-3+ writes after the first real instance (frankyxhl/trinity#66 approval + body checkbox edits) | Claude Code |

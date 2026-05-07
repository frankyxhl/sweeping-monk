# SOP-1101: Codex Resolution Verification

**Applies to:** SWM project
**Last updated:** 2026-05-07
**Last reviewed:** 2026-05-07
**Status:** Active
**Used by:** SWM-1100 step 4

---

## What Is It?

The judgment procedure for deciding whether a single `chatgpt-codex-connector[bot]` comment on a PR has been genuinely addressed. Outputs one of `RESOLVED`, `OPEN`, `NEEDS_HUMAN_JUDGMENT` per Codex thread.

## Why

Per maintainer requirement: a PR is `ready` only when every Codex finding has been (a) replied to or fixed, and (b) the response is substantively reasonable. CI green alone is not sufficient. Without a deterministic rubric, the watchdog will alternately spam `ready` notifications on PRs where Codex objections still stand, or block PRs where the author already addressed the concern.

---

## When to Use

- Once per Codex bot comment encountered during SWM-1100 step 4.
- Re-run on every poll where the PR's `head_sha`, the Codex comment list, or the author response set has changed.

## When NOT to Use

- For comments by humans or other bots — only `chatgpt-codex-connector[bot]` (REST `user.login`) / `chatgpt-codex-connector` (GraphQL `author.login`) gets this rubric.
- For Codex comments older than the latest head_sha that the author has already force-pushed past — those are stale; re-run only against the comment set on the current head.

---

## Steps

1. **Read Codex's PR-body reaction** as the primary status signal — see "Codex PR-Body Reaction Signal" below. This is checked first because it's an explicit, bot-authored verdict and overrides per-thread guesswork.
2. **Gather inputs** — see "Inputs (per Codex comment)" below.
3. **Walk the decision tree** in order; the first matching branch produces the verdict — see "Decision Tree" below.
4. **Apply the substantive-reasonableness heuristics** when the tree reaches step 5.
5. **Emit one record per Codex comment** in the shape under "Output Shape".
6. **Handle edge cases** per the "Edge Cases" section before finalizing the verdict.

---

## Codex PR-Body Reaction Signal (added 2026-05-07)

Codex bot communicates its review state by reacting to the **PR body itself**
(not to its own comments, not to inline review threads). One reaction per state:

| Reaction | Meaning | Watchdog interpretation |
|----------|---------|-------------------------|
| 👀 EYES | "Currently reviewing this head" | Force PR status to `pending` regardless of other signals — the bot says it isn't done |
| 👍 THUMBS_UP | "Reviewed; no new issues / approved" | Strong positive signal; allows a fresh PR with empty CI (paths-ignore) to flip `ready` without waiting out the CI grace window |
| (no reaction) | Codex hasn't engaged yet | Fall through to existing logic (CI grace + thread verdicts) |

If both 👀 and 👍 appear in a transition window (rare — Codex usually swaps them
atomically), 👍 wins.

The reaction is found via the GraphQL `pullRequest.reactions` connection,
filtered on `user.login == "chatgpt-codex-connector[bot]"` (REST) or
`"chatgpt-codex-connector"` (GraphQL — the `[bot]` suffix is REST-only).
The watchdog implementation lives in `swm.classify.codex_pr_body_signal()`
and the status interaction lives in `swm.poll._compute_status()`.

**Why this matters:** before this rule, the watchdog had to infer Codex's
state from "did it post any new comments?" — which conflates "no new comments
because approved" with "no new comments because hasn't looked yet". The PR-body
reaction is unambiguous and authoritative.

---

## Inputs (per Codex comment)

- `comment` — the Codex comment object: `id`, `body`, `path`, `line`, `created_at`, `in_reply_to_id` (if a follow-up).
- `codex_thread` — all subsequent comments in the same review thread (filter by `in_reply_to_id` chain).
- `commits_since_comment` — commits whose `created_at` > `comment.created_at`, plus their file diffs.
- `codex_reactions_on_followups` — reactions on comments that reply to this Codex comment.
- `current_head_sha` — for staleness check.

---

## Thread State Classification (GitHub-side)

Before the decision tree, classify each Codex thread into one of three GitHub-side states using GraphQL `pullRequestReviewThread` fields:

- **A — fresh**: `isOutdated == false` AND no comment in the thread has `in_reply_to_id` pointing to the Codex comment. The author has neither pushed code that invalidates the diff anchor, nor replied.
- **B — outdated**: `isOutdated == true`. The author pushed commits that changed the lines Codex anchored to, so GitHub considers the comment stale. Strong signal that a fix landed (but verify, do not assume).
- **C — replied**: `isOutdated == false` AND there exists at least one comment with `in_reply_to_id == codex_comment.id`. Author addressed Codex via text reply (and possibly also code) but the diff anchor still resolves.

Note: B and C can co-occur when the author both fixed the code AND replied. Treat as B-with-reply (run B's tree branch but use the reply as additional evidence in step 3).

## Decision Tree

```
1. Pre-check: Is the comment older than the current head AND the file/line it points at no longer exists at all in the head (file deleted, function removed)?
   → RESOLVED (reason: "code path removed; comment moot")

2. Classify thread as A / B / C (see "Thread State Classification" above).

3. State B (outdated, author pushed code):
   Diff the change(s) that turned the thread outdated against the original Codex concern.
   ├── The new code addresses the named failure mode (validation added, branch added, type widened, etc.) → RESOLVED (reason: "outdated; commit <sha> addresses Codex's '<one-line concern>'")
   ├── The new code touches the line but does NOT address the concern (cosmetic edit, unrelated refactor) → OPEN (reason: "outdated by unrelated edit; original concern still applies in new diff at <new path:line>")
   └── The new code makes the concern WORSE (e.g., Codex flagged missing null-check, author rewrote without adding it) → OPEN, escalate effective severity per SWM-1102 §C

4. State C (not outdated, author replied via text):
   Apply "Substantively Reasonable" heuristics below to the reply.
   ├── Substantive AND sound (cites code/constraint that makes fix unnecessary, OR points to a tracked issue with concrete plan) → RESOLVED (reason: "author reply <id> addresses concern: <one-line summary>")
   ├── Substantive but depends on judgment outside the diff → NEEDS_HUMAN_JUDGMENT (surface to maintainer; do not auto-resolve)
   ├── Non-substantive (empty, off-topic, hand-wave, "won't fix" without reason) → OPEN (reason: "reply <id> non-substantive")
   └── B-with-reply variant: if the author both fixed code AND replied, run the B branch first; if B yields RESOLVED, the reply is bonus evidence. If B yields OPEN, the reply alone may rescue the verdict via this branch.

5. State A (fresh — no code change, no reply):
   → OPEN (reason: "no author response and no code change since Codex comment created at <ts>")

6. Codex follow-up override (applies on top of any verdict above):
   ├── Codex posted positive follow-up on current head (👍 reaction on the thread OR new comment with "looks good" / "no new issues" / "addressed") → RESOLVED (overrides step 3-5)
   ├── Codex posted negative follow-up (restates concern OR raises a new one tied to the same path:line) → OPEN (overrides any RESOLVED)
   └── No Codex follow-up → keep the step 3-5 verdict.

7. Stage 1.5 sync (only if CLAUDE.md has Stage 1.5 active AND step 6 verdict is RESOLVED):
   Read GraphQL: query the thread node's current `isResolved`.
   ├── isResolved == true already → no action; record "github_already_resolved": true
   └── isResolved == false → call `resolveReviewThread` mutation with the thread node ID. Log mutation result and the verdict reason. Record "github_resolve_synced": true.

8. Stage 1.5 unresolve (only if a later poll downgrades a previously-RESOLVED verdict back to OPEN):
   Call `unresolveReviewThread` to keep GitHub state in sync with local verdict. Log the reason for downgrade.
```

**Authority shift (2026-05-07):** Step 3 and step 4 used to be "wait for Codex to re-review." That gate has been removed — the watchdog is now the judge of whether a fix or reply addresses the concern. Codex's follow-up (step 6) is an override, not a precondition. Reason: Codex bot does not consistently re-review after force-push, leaving PRs stuck pending indefinitely.

---

## "Substantively Reasonable" Heuristics

A reply counts as substantive when ALL hold:

- It names the specific concern Codex raised (path / mechanism / failure mode) — not a generic "thanks for the review".
- It provides at least one of: a counter-fact about the code, a binding constraint (compatibility, perf, scope), a deferred-tracking pointer (issue / TODO with concrete plan), or a corrected reading of Codex's claim.
- It does not contradict the diff (e.g., claiming "we already validate input" when the diff shows no validation).
- It does not promise a future fix without (a) a tracked issue, or (b) a constraint explaining why this PR is not the right place.

When in doubt between OPEN and RESOLVED, choose `NEEDS_HUMAN_JUDGMENT` and surface it in the report so the maintainer decides.

---

## Output Shape (one record per Codex comment)

```json
{
  "comment_id": 3201472766,
  "path": ".github/workflows/test.yml",
  "line": 20,
  "codex_severity": "P2",
  "effective_severity": "P3",
  "verdict": "OPEN",
  "verdict_reason": "no author response yet; Codex P2 demoted to P3 by SWM-1102 §B (no branch protection)",
  "evidence": {
    "code_changed": false,
    "author_replied": false,
    "codex_followed_up": false,
    "codex_reaction": null
  }
}
```

---

## Edge Cases

- **Force push that removes the file**: treat as RESOLVED (step 1) only if the diff against base no longer contains the line; do not assume force-push erases concerns about behavior the new diff still exhibits.
- **Multi-comment threads**: each Codex comment in the thread is verified independently. A later Codex comment can either replace or reinforce an earlier one — verify them in chronological order and let the latest verdict win for that path:line.
- **Codex marks the review state as `APPROVED`** (top-level): this counts as a 👍 across all open threads on the same head and resolves any thread whose verdict was OPEN solely because Codex hadn't followed up.
- **Author dismisses the Codex review**: dismissal is metadata, not substance. Do not auto-resolve. Run the tree as if the dismissal did not happen.

---

## Examples

### PR #49 (frankyxhl/trinity, 2026-05-07) — applied retroactively after rule update

- Codex bot reacted on PR body with 👍 THUMBS_UP at 12:48:44Z (≈1 min after author pushed `c476c877`).
- Codex inline P2 thread (paths-ignore vs required check): GitHub-side `isResolved=false`, `isOutdated=false`, author posted substantive inline reply.
- Step 1 (PR-body signal): `approved` → does NOT force pending; allows downstream rules to flip ready when threads also resolve.
- Step 3 (decision tree): State C + substantive reply (cites `gh api` evidence) → RESOLVED.
- Step 7 (Stage 1.5 sync): GitHub thread `isResolved=false` + local verdict RESOLVED → `resolveReviewThread` mutation fired → GitHub now `isResolved=true`.
- Final: PR ready, 1 thread RESOLVED, GitHub state synced.

### PR #50 (frankyxhl/trinity, 2026-05-07) — pure docs PR, paths-ignore active

- Codex bot reacted on PR body with 👍 THUMBS_UP at 14:11:08Z (≈2 min after PR opened).
- Zero inline review threads (Codex saw nothing to flag in the diff).
- `statusCheckRollup` empty (paths-ignore matched 100% of the diff per PR #49's optimization).
- Step 1 (PR-body signal): `approved`.
- No threads to walk; CI grace window doesn't matter because Codex 👍 + empty CI → ready immediately.
- Final: PR ready without waiting for any further input.

## Change History

| Date | Change | By |
|------|--------|----|
| 2026-05-07 | Initial version | Claude Code |
| 2026-05-07 | Authority shift: watchdog now judges fix/reply quality directly (B/C → RESOLVED on its own judgment); Codex follow-up demoted from gate to override. Added Stage 1.5 sync (resolveReviewThread) as steps 7-8. Added explicit A/B/C thread-state classification block. | Claude Code |
| 2026-05-07 | Added "Codex PR-Body Reaction Signal" as primary status check (step 1): 👀 EYES → force pending, 👍 THUMBS_UP → allow ready even within CI grace. Codex reacts on the PR *body*, not on its own comments — this is the strongest authoritative signal of its review state. Implementation: `swm.classify.codex_pr_body_signal()` + `swm.poll._compute_status()`. | Claude Code |

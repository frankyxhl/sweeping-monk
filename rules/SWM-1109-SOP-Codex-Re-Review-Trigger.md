# SOP-1109: Codex Re-Review Trigger

**Applies to:** SWM project
**Last updated:** 2026-05-08
**Last reviewed:** 2026-05-08
**Status:** Active
**Depends on:** SWM-1100 (poll cycle), SWM-1101 (thread resolution), SWM-1103 (Stage 1.5 writes)

---

## What Is It?

When an author pushes a new commit to address a Codex finding, Codex does not automatically re-review the PR. This SOP defines when the watchdog posts `@codex review` as a PR comment to trigger a fresh Codex review of the current head commit.

## Why

`swm` requires "Codex's last action on the current head is positive" before calling a PR `READY` (CLAUDE.md Stage 1.5 contract). Without a re-review trigger, a PR where Codex reviewed an old commit will stay `PENDING` indefinitely — even after the author has fixed every Codex finding. This SOP closes that gap.

---

## When to Use

- `--sync` flag is passed to `swm poll` (opt-in write action).
- Codex has at least one prior review on this PR.
- The latest Codex review's `commit.oid` ≠ current `head_sha`.
- Codex's PR-body signal is not `reviewing` (👀).
- The prior poll for the same `(repo, pr, head_sha)` has not already set `codex_rereview_triggered = true` (idempotency guard).

## When NOT to Use

- Without `--sync` — read-only poll mode must never post.
- When Codex has 👀 on the PR body — Codex is already actively reviewing the head.
- When there are no prior Codex reviews — brand-new PR waiting on first review; do not spam.
- When `codex_rereview_triggered` is already `true` for the current head in the stored `PollRecord` — one trigger per `(PR, head_sha)`, ever.

---

## Steps

1. **Fetch PR reviews** — call `GhClient.pr_reviews(repo, pr)` to get top-level reviews with `author.login` and `commit.oid`.

2. **Detect need** — call `classify.codex_needs_rereview(reviews, head_sha=head_sha, codex_signal=codex_signal)`. Returns `True` only when all trigger conditions above hold.

3. **Check idempotency** — read the prior `PollRecord` from the state store for `(repo, pr)`. If `prior.head_sha == head_sha` and `prior.codex_rereview_triggered`, skip to step 5. *(Accepted TOCTOU risk: two concurrent `swm poll --sync` processes could both pass this check before either writes. Blast radius is low — Codex ignores duplicate `@codex review` if already reviewing. No lock is added.)*

4. **Post comment** — call `GhClient.post_issue_comment(repo, pr, "@codex review")`. This executes:
   ```bash
   gh api repos/<owner>/<repo>/issues/<N>/comments --method POST -f body="@codex review"
   ```
   Log the call with `(repo, pr, head_sha)` in the watchdog report.

   On `GhCommandError` (network error, rate limit, 403): log the failure in the watchdog report, leave `codex_rereview_triggered = False` (so the next poll retries), and continue the poll normally. Do not abort.

5. **Record trigger** — set `PollRecord.codex_rereview_triggered = True` on the record being persisted only on success.

6. **Report** — print to console:
   ```
   Re-review triggered: posted @codex review on <repo>#<N> (Codex last reviewed a stale head)
   ```

---

## Stage 1.5 Permission Scope

This write is part of Stage 1.5. Strictly permitted:

- `post_issue_comment` may only post the literal string `@codex review`. No other comment text.
- One comment per `(PR, head_sha)` only.
- Only fires when `--sync` is active.

Forbidden under this SOP:

- Posting any other comment text.
- Posting more than once per `(PR, head_sha)` across polls.
- Firing without `--sync`.

---

## Examples

**Normal trigger (stale head):**
```
swm poll frankyxhl/trinity --sync
# Codex last reviewed 41174b98, current head is 2cd4fd4f
# → posts "@codex review", sets codex_rereview_triggered=true
Re-review triggered: posted @codex review on frankyxhl/trinity#71 (Codex last reviewed a stale head)
```

**Idempotency (second poll, same head):**
```
swm poll frankyxhl/trinity --sync
# prior PollRecord has codex_rereview_triggered=true for 2cd4fd4f
# → skips post_issue_comment, no duplicate comment
```

**No trigger (Codex reviewing):**
```
swm poll frankyxhl/trinity --sync
# codex_signal="reviewing" (👀 on PR body)
# → codex_needs_rereview returns False, no comment posted
```

---

## Change History

| Date       | Change          | By          |
|------------|-----------------|-------------|
| 2026-05-08 | Initial version | Claude Code |

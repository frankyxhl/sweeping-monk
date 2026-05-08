# CHG-1110: Codex Re-Review Trigger Implementation

**Applies to:** SWM project
**Last updated:** 2026-05-08
**Last reviewed:** 2026-05-08
**Status:** Proposed
**Date:** 2026-05-08
**Requested by:** Frank Xu
**Priority:** Medium
**Change Type:** Normal
**Targets:** `swm/classify.py`, `swm/gh.py`, `swm/models.py`, `swm/poll.py`, `swm/cli.py`, `CLAUDE.md` (§Stage 1.5, §Future Permission Stages, §Suggested Local Commands, §Non-Goals), `rules/SWM-1100` (§Hard Constraints), `rules/SWM-0000` (index), `SWM-1109` (new)

---

## What

Extend `swm poll --sync` to detect when Codex reviewed a previous commit but not the current head, and automatically post `@codex review` as a PR comment to trigger re-review. Adds a new Stage 1.5 write permission (`post_issue_comment`) and documents the operational procedure in SWM-1109.

| Component | Change |
|-----------|--------|
| `classify.py` | New `codex_needs_rereview(reviews, *, head_sha, codex_signal) → bool` |
| `gh.py` | New `pr_reviews(repo, pr) → list[dict]`; new `post_issue_comment(repo, pr, body) → None` |
| `models.py` | New `PollRecord.codex_rereview_triggered: bool = False`; populate existing `codex_last_review_head: str \| None` and `codex_last_review_at: datetime \| None` from reviews |
| `poll.py` | Wire detection + idempotency guard + trigger into `poll_pr()` / `poll()`; populate `codex_last_review_head` |
| `cli.py` | Print re-review trigger line in `poll_cmd` output |
| `CLAUDE.md` | Extend Stage 1.5 permissions block with `post_issue_comment` bullet |
| `rules/SWM-1109` | New SOP documenting trigger conditions, idempotency, and Stage 1.5 scope |

## Why

`swm` calls a PR `READY` only when "Codex's last action on the current head is positive" (CLAUDE.md). After an author pushes a fix commit, Codex does not auto-re-review. The watchdog therefore stays `PENDING` indefinitely on a PR that is functionally complete. Session evidence: PR #71 (`frankyxhl/trinity`) had all Codex threads resolved via `--sync` but remained `PENDING` because Codex's last review was on `41174b98`, not the current head `2cd4fd4f`.

## Impact Analysis

- **Systems affected:** `swm` Python package (5 files), `CLAUDE.md` Stage 1.5 section, `rules/` (new SWM-1109, updated SWM-0000 index).
- **New write permission:** `gh api repos/.../issues/.../comments --method POST` — strictly scoped to body `"@codex review"`, once per `(PR, head_sha)`. Guarded by `--sync` opt-in and idempotency check against `StateStore`.
- **Rollback plan:** Revert Python changes; remove `post_issue_comment` from `GhClient`; remove the Stage 1.5 bullet from `CLAUDE.md`. SWM-1109 stays as historical record. In-flight re-review comments already posted on GitHub are harmless (Codex ignores duplicate `@codex review` if it is already working).

## Implementation Plan

1. **`classify.codex_needs_rereview()`** — detection logic, TDD. Tests in `tests/test_classify.py`.
2. **`GhClient.pr_reviews()` + `GhClient.post_issue_comment()`** — two new methods in `swm/gh.py`; extend `FakeGhClient` in `tests/conftest.py`. Tests in `tests/test_gh.py`.
3. **`PollRecord.codex_rereview_triggered`** — new `bool = False` field in `swm/models.py`. Tests in `tests/test_models.py`.
4. **Wire into `poll_pr()` / `poll()`** — fetch reviews, run detection, enforce idempotency, call trigger, update record. Tests in `tests/test_poll.py`.
5. **`cli.py` output** — print trigger confirmation line when `codex_rereview_triggered` is set.
6. **Docs + CLAUDE.md** — SWM-1109 already created; add Stage 1.5 bullet to `CLAUDE.md`; update SWM-0000 index.

Full test-first detail for each step lives in the session plan at `docs/plans/2026-05-08-codex-rereview-trigger.md`.

## Approval

- [ ] Approved by: <reviewer> on <date>

---

## Change History

| Date       | Change          | By          |
|------------|-----------------|-------------|
| 2026-05-08 | Initial proposal | Claude Code |

# REF-1102: Severity Promotion Rules

**Applies to:** SWM project
**Last updated:** 2026-05-07
**Last reviewed:** 2026-05-07
**Status:** Active
**Used by:** SWM-1100 step 4, SWM-1101 step 3 output

---

## What Is It?

Reference table for translating an external severity label (chiefly `chatgpt-codex-connector[bot]` P1/P2/P3 badges) into the **effective local severity** the watchdog uses to decide `ready` vs `blocked`. Severity is contextual: the same finding can be P2 in a repo with branch protection and P3 in one without, because its blast radius differs.

## Why

CLAUDE.md says any P1/P2 finding forces `blocked`. Mechanically applying Codex's badge would over-block PRs in repos where the named risk cannot trigger (e.g., paths-ignore conflicting with required checks in a repo that has no required checks). Conversely, a Codex P3 may need promotion if local context makes the failure mode immediate (e.g., production already broken).

---

## A. Default mapping

| Codex badge | Default effective severity |
|-------------|----------------------------|
| P1 | P1 |
| P2 | P2 |
| P3 | P3 |
| (no badge) | infer from body — default P3 unless body names a concrete failure mode |

Default applies until one of sections B / C / D triggers.

---

## B. Demotions (Codex severity ≥ effective severity)

Demote when the named risk has no current channel to manifest.

| Trigger | Effect | Notes |
|---------|--------|-------|
| Codex flags a CI/required-check coupling AND the base branch has no `branches/<base>/protection` rule | P2 → P3 | Verified via `gh api repos/<o>/<r>/branches/<base>/protection` returning 404 "Branch not protected". Re-promote on next poll if protection appears. |
| Codex flags a public API breakage AND the symbol is internal-only (no exports, no consumers found via grep across known dependents) | P2 → P3 | Demotion contingent on a real dependency check — do not demote on intuition. |
| Codex flags a perf regression AND the affected code path is gated behind a flag that defaults off | P2 → P3 | Re-promote when the flag flips on by default. |
| Codex flags a security finding AND the input is provably unreachable from untrusted sources (proven by grep, not assumed) | P1 → P2, or P2 → P3 | Demote one step only, never further. The proof must hold for the current head. |
| Codex flags a missing test AND the behavior under test is unchanged in the diff | P2 → P3 | "No new behavior, no new test required" — but only if the diff truly does not modify the behavior. |

**Demotion always logs the reason in the verdict so the maintainer can audit.**

---

## C. Promotions (Codex severity < effective severity)

Promote when local context makes the risk worse than Codex realized.

| Trigger | Effect | Notes |
|---------|--------|-------|
| Codex P3 names a behavior also flagged by an open INC / incident in the repo within the last 30 days | P3 → P2 | The repeat occurrence means the risk is active, not theoretical. |
| Codex P3 touches a path listed in a `CRITICAL_PATHS` allowlist (per SWM config, when present) | P3 → P2 | Default config has no critical paths until configured. |
| Codex P2 touches data migration / destructive SQL / schema change | P2 → P1 | Data loss class is always P1 per CLAUDE.md "Review Focus". |
| Codex finding correlates with a failing required CI check that names the same path | P3 → P2, P2 → P1 | The CI failure converts theoretical into observed. |

---

## D. No-change cases (severity stays as-is)

Do **not** demote or promote when:

- The trigger condition is "I think it's probably fine" with no concrete check.
- Branch protection state is unknown (404 indistinguishable from rate-limit error). Treat unknown as "protection present" for safety.
- The PR is by an external contributor and the local watchdog has only partial signal.

When in doubt, leave severity at the Codex value. The user has explicitly said Codex is treated as an authoritative second reviewer.

---

## E. Worked example — PR #49 (frankyxhl/trinity, 2026-05-07)

- **Codex finding**: P2, line 20 of `.github/workflows/test.yml`, "paths-ignore makes required Test checks unsatisfiable for docs-only PRs".
- **Local check**: `gh api repos/frankyxhl/trinity/branches/main/protection` → HTTP 404 "Branch not protected".
- **Rule applied**: Section B, row 1 (no branch protection → demote one step).
- **Effective severity**: P3.
- **Re-promotion trigger**: any future poll where branch protection becomes present and Test is listed as a required check.
- **Watchdog action**: Codex thread still OPEN per SWM-1101 (no author response, no fix), so PR remains `pending`. The demotion only changes whether this single finding alone would force `blocked` — at P3 it does not.

---

## Change History

| Date | Change | By |
|------|--------|----|
| 2026-05-07 | Initial version — codify branch-protection-aware demotion and migration-class promotion | Claude Code |

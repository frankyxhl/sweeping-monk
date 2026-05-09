# CHG-1112: Notify maintainer on positive ready/approved transitions in `swm poll`

**Applies to:** SWM project
**Last updated:** 2026-05-09
**Last reviewed:** 2026-05-09
**Status:** Proposed
**Date:** 2026-05-09
**Requested by:** Frank Xu / Codex (GitHub issue #9)
**Priority:** P1 — High
**Change Type:** Normal
**Targets:** New `swm/notify.py` (~50 LoC: `NotificationRecord` Pydantic model + `detect_positive_transition()` predicate + `format_suggested_action()`). Edits in `swm/poll.py` (~10 LoC: invoke detector after the CHG-1107 short-circuit branch; emit a single `notify:` line on stdout; append to `notifications.jsonl`). Edits in `swm/state.py` (~6 LoC: `append_notification()` method that writes to the existing top-level `notifications_log` path already declared at `swm/state.py:51`). New tests in `tests/test_notify.py` (~100 LoC). One-line `README.md` reference under the watchdog section per issue #9 acceptance criterion 6.

---

## What

A deterministic Python transition detector keyed off the existing `PollRecord.state_key()` machinery. No new gh calls, no LLM, no new state file: writes to the already-declared `notifications.jsonl`. Four named transitions — every dimension the detector keys on is also part of `state_key`, so the detector cannot rely on a signal that the CHG-1107 short-circuit suppresses (round-1 review caught a fifth proposed transition that violated this invariant; see Change History):

```python
# swm/notify.py
Transition = Literal[
    "first-ready",            # prior is None and new.status == READY
    "blocked-to-ready",       # prior.status == BLOCKED and new.status == READY
    "pending-to-ready",       # prior.status == PENDING and new.status == READY
    "ready-after-head-bump",  # both READY, head_sha differs (re-notify on new head)
]

class NotificationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ts: datetime
    repo: str
    pr: int
    title: str | None
    head_sha: str
    transition: Transition
    suggested_action: str   # e.g. `swm approve frankyxhl/swm 9 --reason 'CI green, Codex approved'`
    summary: str            # one line, ≤ 200 chars; references the changed dimension

def detect_positive_transition(prior: PollRecord | None, new: PollRecord) -> Transition | None:
    if new.status != Status.READY:
        return None
    if prior is None:
        return "first-ready"
    if prior.status == Status.BLOCKED: return "blocked-to-ready"
    if prior.status == Status.PENDING: return "pending-to-ready"
    # READY → READY only — Codex PR #11 review #3212804778 caught that an
    # unguarded `prior.head_sha != new.head_sha` mis-fires `ready-after-head-bump`
    # for ERROR/SKIPPED → READY recoveries. The guard makes the head-bump
    # transition strictly READY→READY; recovery is deferred (see §Out-of-scope).
    if prior.status == Status.READY and prior.head_sha != new.head_sha:
        return "ready-after-head-bump"
    return None
```

**Detector ↔ `state_key` coupling invariant:** every dimension the detector branches on (`status`, `head_sha`) is part of `state_key` (`swm/models.py:99-107`). If a future detector branch keys on a new dimension, that dimension MUST also be added to `state_key` in the same PR — otherwise the CHG-1107 short-circuit will fire first and render the branch unreachable.

Wiring in `swm/poll.py` (placed AFTER the existing CHG-1107 short-circuit, so identical `state_key` polls never reach the detector — that single ordering invariant kills the spam case from issue #9 criterion 5):

```python
# Inside the per-PR body of `run_poll(...)`, AFTER the existing CHG-1107
# short-circuit has computed `no_change` and AFTER `store.append_poll(record)`.
# The detector branch is gated on `not no_change` so identical state_key polls
# never reach it; the surrounding loop continues to the next PR regardless
# (Codex review #3212762796: do NOT `return` from the per-PR body — that
# would stop the whole repo poll after the first unchanged PR; the existing
# `is_no_change`-aggregated render in cli.py is preserved).
store.append_poll(record)
if not no_change:
    transition = detect_positive_transition(prior, record)
    if transition is not None:
        note = NotificationRecord.from_transition(prior, record, transition)
        store.append_notification(note)
        short_sha = record.head_sha[:7]
        typer.echo(
            f"notify: {repo}#{record.pr} {transition} "
            f"@ {short_sha} -> {note.suggested_action}"
        )
```

State layout unchanged from today — `state/notifications.jsonl` already declared at `swm/state.py:51`; this CHG only adds the writer.

## Why

Issue #9 is the P1 user-visible payoff for the existing CHG-1107 + SWM-1108 + CHG-1111 chain. Today the watchdog computes `READY` correctly but emits the same dashboard card every poll once a PR goes green; the maintainer has to read every card to spot the *moment* a PR became actionable. Pushing the transition into a one-line `notify:` event (and a JSONL audit row) means the cron prompt can `grep -q "^notify:"` to surface only true ready/approved transitions, and a downstream tool (or SWM-1108 daemon's inbox) can ingest the JSONL.

The detector intentionally lives at the `swm poll` layer, NOT in the SWM-1108 daemon: every existing `swm poll` consumer (cron, manual run, CI, the future daemon's poll call from CHG-1108) gets the same notification semantics for free. SWM-1108's daemon already calls `swm poll` per CHG-1107's short-circuit pattern, so notification fan-out is automatic.

## Out of scope

- Adding `review_decision` to `PollRecord` / `state_key` / classifier. Issue #9 mentions `reviewDecision=APPROVED` as a "qualifying approval signal". Verified ground truth (round-1 review caught a false claim in the previous draft): `reviewDecision` is fetched in `swm/gh.py:97` but is NOT consumed by `_compute_status` in `swm/poll.py:215-253` — only `(ci_class, P1/P2 OPEN thread verdicts, codex_pr_body_signal)` drive status. Consequence: a human-clicked GitHub Approve does not, by itself, flip status to READY in this watchdog's model. This is a real coverage gap of the present CHG. The reason it's deferred rather than fixed here: making `reviewDecision` a status driver requires (a) extending `_compute_status`, (b) extending `state_key` to include it (otherwise the CHG-1107 short-circuit hides the transition — same trap as the dropped `codex-approved-on-ready` branch), and (c) coordination with CHG-1107's contract. That is a separate CHG, not a one-line addition. This CHG ships the four transitions that ARE expressible with the current `state_key` and explicitly leaves the APPROVED-only path open.
- The `codex-approved-on-ready` transition that appeared in round-1 of this CHG. Dropped because `state_key` excludes `codex_pr_body_signal`, making the branch unreachable in production (CHG-1107's short-circuit returns first). Practical coverage loss is small: Codex `"approved"` typically follows code change → head bump → status flip, all of which are still caught by `ready-after-head-bump` / `pending-to-ready` / `blocked-to-ready`. If the Codex-flips-on-an-already-READY-PR case (no head bump, no CI flip, no thread change) turns out to matter operationally, the fix is a separate cross-CHG that adds `codex_pr_body_signal` to `state_key` together with the new branch.
- Recovery transitions where `prior.status ∈ {ERROR, SKIPPED}` and `new.status == READY`. Codex review #3212804778 (PR #11, post-implementation) flagged that the head-bump branch must be guarded with `prior.status == Status.READY`, otherwise an `ERROR → READY` or `SKIPPED → READY` transition mis-fires as `ready-after-head-bump`. The implementation gates the branch and returns `None` for these cases — the maintainer learns nothing from the watchdog when an erroring PR recovers. A dedicated `recovered-to-ready` transition is a separate one-line CHG (no `state_key` change needed since `status` is already a member); deferred until operational signal that ERROR/SKIPPED → READY is common enough to matter.
- A `swm notifications` CLI subcommand for listing/dismissing entries. The JSONL is greppable from day 1; a CLI front door is SWM-1108's `swm inbox` family.
- GitHub-side notification (comment, review). Forbidden by Stage 1 and Stage 1.5.
- Rich content (PR diff summary, Codex thread excerpts, review-decision delta). The `summary` field is one line, ≤ 200 chars, sourced from the deterministic transition + state-key delta; LLM-generated summaries belong to CHG-1111's adapter.
- Notification dedupe across daemon restarts beyond what `state_key` already provides. The append-only JSONL record IS the dedupe key — repeated polls with the same state_key never reach the detector (CHG-1107 short-circuit).
- Pruning `notifications.jsonl`. Same audit-trail bias as `polls.jsonl`.

## Compatibility

- `swm poll` exit codes unchanged.
- `polls.jsonl` schema unchanged; same record shape.
- `notifications.jsonl` already declared at `swm/state.py:51` — this is the first writer; no migration.
- All existing `swm` subcommands unchanged.
- CHG-1107's short-circuit invariant is preserved: identical `state_key` polls still emit only `no change: ...` and never trigger a notification (single-source-of-truth: the same `prior.state_key() == new.state_key()` check guards both).
- Stage 1 / Stage 1.5 permission model unchanged — no GitHub writes; output is local stdout + local JSONL.
- The new `notify:` stdout line is additive; existing cron prompts that don't grep for it see one extra line per genuine transition (and zero on no-change polls). Updated cron prompt example will be added to README.

## Risks (each backed by a named test)

| Risk | Mitigation | Test |
|------|-----------|------|
| Detector fires on every poll for a long-running READY PR (notification spam) | Detector is invoked AFTER the CHG-1107 short-circuit branch; identical `state_key` polls never reach it. Within the changed-state path, `ready-after-head-bump` requires `head_sha != prior.head_sha`; `codex-approved-on-ready` requires `codex_pr_body_signal` to actually flip. | `test_no_notification_when_state_key_unchanged`; `test_no_notification_when_ready_polled_repeatedly_with_same_head` |
| First-ever poll for a PR already ready misses notification because `prior is None` | `prior is None and new.status == READY` → `"first-ready"` (explicit branch in detector) | `test_first_observation_of_ready_emits_first_ready` |
| Status drop from READY → BLOCKED followed by BLOCKED → READY at a new head silently re-notifies as if nothing happened | `blocked-to-ready` transition fires on the rebound regardless of head_sha; `ready-after-head-bump` fires when both polls are READY but head differs. The two are mutually exclusive in the detector branch order. | `test_blocked_to_ready_at_new_head_emits_blocked_to_ready` (asserts transition is `blocked-to-ready` not `ready-after-head-bump`) |
| `notify:` line collides with `no change:` line in cron grep | Distinct prefix; `no change:` is suppressed (return early) before the notify branch executes; pinned subprocess test asserts a single `^notify:` line per real transition and zero on no-change polls. | `test_swm_poll_emits_at_most_one_notify_line_per_invocation` |
| `suggested_action` contains a non-shell-safe value (e.g. quotes in PR title) | `suggested_action` is constructed via `format_suggested_action(repo, pr, reason)` using `shlex.quote()` for the reason; PR title is not embedded — `summary` carries the title, separately. | `test_suggested_action_quotes_unsafe_reason` |
| A future detector branch keys on a dimension not in `state_key`, becoming dead code (the trap that killed round-1's `codex-approved-on-ready` branch) | Documented invariant in §What: every dimension the detector branches on must also be in `state_key`. Test asserts the current four-branch detector touches only `(status, head_sha)` — both members of `state_key`. | `test_detector_only_branches_on_state_key_dimensions` |
| `notifications.jsonl` write fails (disk full, permissions) and silently drops the notification | `append_notification` propagates IOError; the poll command surfaces the failure on stderr but does NOT block append of the underlying poll record (that already happened on line 4 of the wiring snippet — `store.append_poll(new_record)` is called before the detector). Audit trail of polls is unbroken; only the notification side-channel is lost. | `test_notification_write_failure_does_not_drop_poll_record` |

## Acceptance

- [ ] `swm/notify.py` defines `NotificationRecord` (`extra="forbid"`) + `detect_positive_transition()` + `format_suggested_action()` + the four-value `Transition` literal (`first-ready`, `blocked-to-ready`, `pending-to-ready`, `ready-after-head-bump`).
- [ ] `swm/state.py` adds `append_notification(NotificationRecord) -> None` writing to the existing `notifications_log` path. No other state.py changes.
- [ ] `swm/poll.py` invokes the detector exactly once per state-changed poll; never invokes it on no-change polls (same predicate guards CHG-1107's short-circuit and the detector branch).
- [ ] First `swm poll` for a PR that is already READY emits one `notify: ... first-ready ...` stdout line and one `notifications.jsonl` row.
- [ ] `pending → ready` and `blocked → ready` transitions each emit exactly one notification with the correct `transition` field.
- [ ] `ready → ready` polls with the same `state_key` emit zero notifications and zero stdout `notify:` lines (CHG-1107 short-circuit fires).
- [ ] `ready → ready` polls where `head_sha` differs emit one `ready-after-head-bump` notification.
- [ ] `suggested_action` is shell-safe — `shlex.quote()` is used for the `--reason` payload; PR titles never appear in `suggested_action`.
- [ ] Static check `test_detector_only_branches_on_state_key_dimensions` asserts the detector keys only on dimensions present in `PollRecord.state_key()` (lock the round-1 invariant in CI).
- [ ] Implementer-check: a deterministic subprocess test runs `swm poll` against a fixture and asserts `grep -c "^notify:"` returns 1 on a transition fixture and 0 on a no-change fixture.
- [ ] `pytest` ≥ 80% gate; new modules ≥ 90% line coverage.
- [ ] `af validate --root .` clean.
- [ ] README adds a one-paragraph "When `swm poll` emits `notify:`" block per issue #9 acceptance criterion 6.

## Approval

- [x] Trinity fast-review (preset `{glm, deepseek}` per `~/.claude/trinity.json`; ≥ 9.0 mean, no convergent FAIL). **Round-2 PASS: GLM 10.00, DeepSeek 9.00, mean 9.50 (2026-05-09).**
- [ ] Approved by: <reviewer> on <date>

---

## Change History

| Date | Change | By |
|------|--------|----|
| 2026-05-09 | Initial proposal — deterministic positive-transition detector on top of CHG-1107's short-circuit; five named transitions. | Claude Code |
| 2026-05-09 | Round-1 fixes per trinity fast-review (DeepSeek 7.25 / FAIL, GLM 8.60 / PASS-with-P3, mean 7.93 — gate fail). Convergent findings: (1) `codex-approved-on-ready` was dead code because `state_key` excludes `codex_pr_body_signal` — CHG-1107's short-circuit returns before the detector reaches the branch. **Dropped the branch**; documented the gap in §Out-of-scope; added an explicit detector↔state_key invariant + a CI-enforced `test_detector_only_branches_on_state_key_dimensions` so a future contributor cannot re-introduce the same bug. (2) The previous draft falsely claimed `_compute_status` reads `reviewDecision`. Verified false against `swm/poll.py:215-253`. **Rewrote** the §Out-of-scope rationale honestly: this CHG does not detect human GitHub Approve clicks that don't already flip status; the fix requires extending classifier + `state_key` and is its own CHG. Trade-off recorded explicitly so issue #9's APPROVED-only scenario isn't silently dropped. | Claude Code |
| 2026-05-09 | Codex PR-review fixes (PRs #10 + #11). (1) Comment #3212762796 on PR #10: §What pseudocode used `return` inside the per-PR body, which would stop `swm poll` after the first unchanged PR. Rewrote to mirror the actual implementation pattern (`store.append_poll(record); if not no_change: ...`) — gate, don't return; the surrounding `run_poll` loop continues to the next PR regardless. (2) Comment #3212804778 on PR #11: real implementation bug — when `prior.status ∈ {ERROR, SKIPPED}` and `new.status == READY` at a new head, the detector falsely returns `ready-after-head-bump`. Added the `recovered-to-ready` deferral to §Out-of-scope; the head-bump branch in `swm/notify.py` will be guarded with `prior.status == Status.READY` in PR #11 with a regression test. | Claude Code |

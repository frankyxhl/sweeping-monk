# sweeping-monk

A local, read-only Pull Request review watchdog. Watches a GitHub repository,
classifies each open PR's status by combining

- CI state (with a 5-minute grace window for paths-ignore vs runner warm-up)
- the Codex bot's PR-body reaction signal (👀 reviewing / 👍 approved)
- inline review-thread verdicts judged via the [SWM-1101](rules/SWM-1101-SOP-Codex-Resolution-Verification.md) decision tree

then renders a `rich`-table dashboard so the maintainer knows at a glance which
PRs are ready, blocked, or pending.

Stage 1.5 capability: when a Codex thread is locally judged `RESOLVED`,
`swm poll --sync` optionally calls GitHub's `resolveReviewThread` mutation to
keep the GitHub UI in sync. No comments, no reviews, no merges — the human
stays in charge.

When `swm poll` emits `notify:` (CHG-1112): exactly once per positive
ready/approved transition — `first-ready` (first poll for a PR that lands
READY), `pending-to-ready`, `blocked-to-ready`, or `ready-after-head-bump`
(both polls READY at differing head SHAs). Repeated polls at the same
`state_key` short-circuit (CHG-1107) and emit zero `notify:` lines, so a
cron pipeline can `grep -q "^notify:"` to surface only true transitions.
Each notification also lands in `state/notifications.jsonl` for audit.

## Quickstart

```bash
git clone git@github.com:frankyxhl/sweeping-monk.git
cd sweeping-monk
uv venv && uv pip install -e ".[dev]"

.venv/bin/pytest                              # 99 tests, ~94% coverage
.venv/bin/swm poll <owner>/<repo>             # Stage 1: read-only
.venv/bin/swm poll <owner>/<repo> --sync      # Stage 1.5: also resolve threads
.venv/bin/swm dashboard <owner>/<repo>        # render latest poll for each PR
.venv/bin/swm history <owner>/<repo> --pr N   # status timeline for one PR
.venv/bin/swm summary <owner>/<repo>          # one-row-per-PR table
```

`gh` CLI must be authenticated (`gh auth status`). Stage 1.5 needs an extra
Bash allowlist for the `resolveReviewThread` mutation — see
[CLAUDE.md § Stage 1.5 Permission Model](CLAUDE.md#stage-15-permission-model-active-as-of-2026-05-07).

## Example dashboard

```
frankyxhl/trinity #49  —  ci: skip Test workflow on docs-only PRs
╭──────────────┬───────────────────────────────────────────────────────────╮
│ 🚦 Status    │ 🟢 READY                                                  │
│ 🔖 Head      │ 📌 c476c877                                               │
│ ⚙️ CI        │ ✅ ubuntu-latest  ✅ macos-latest                         │
│ 🔀 Merge     │ ✅ clean                                                  │
│ 🤖 Codex bot │ 👍 approved                                               │
├──────────────┼───────────────────────────────────────────────────────────┤
│ 🔍 Findings  │ 1 of 1 resolved                                           │
│   #1         │ ✅ RESOLVED  P2→P3  test.yml:31                           │
│              │ Keep required Test checks satisfiable                     │
│              │ • 💬 author reply #3201506576 — substantive               │
│              │ • 📝 commit c476c877 — added 11-line FOOT-GUN doc comment │
│              │ • ⬇️ severity demoted: main has no branch protection      │
│              │ • 🔗 Stage 1.5 resolveReviewThread mutation               │
╰──────────────┴───────────────────────────────────────────────────────────╯
```

Color/emoji grammar is consistent across rows: 🟢/✅ for ready/resolved/success,
🟡/⏳ for pending/open/in-progress, 🔴/❌ for blocked/failure. All status icons
sit in column-1 so a vertical scan communicates the entire health snapshot.

## How it judges a PR

| Signal | What the watchdog reads | What it does |
|--------|-------------------------|--------------|
| Codex 👀 EYES on PR body | `pullRequest.reactions` GraphQL | Force `pending` (the bot says it isn't done) |
| Codex 👍 THUMBS_UP on PR body | same | Allow `ready` even when CI is empty due to paths-ignore |
| Inline review thread, A/B/C state | `reviewThreads` GraphQL — A=fresh, B=outdated, C=replied | Run SWM-1101 step 3-5 to reach a `RESOLVED` / `OPEN` / `NEEDS_HUMAN_JUDGMENT` verdict |
| Reply substantiveness | regex over the reply body — must cite a concrete identifier (commit SHA / file / API) and clear deflection patterns | Decide whether a State-C reply addresses the concern |
| Branch protection | `repos/<o>/<r>/branches/<b>/protection` (404 → no protection) | Demote a Codex P2 to P3 when its required-check coupling can't actually trigger (SWM-1102 §B) |
| CI rollup | `pr.statusCheckRollup` | Empty + within 5 min of `updatedAt` → in_progress; past 5 min → absent |

The "AI judgment" (substantively-reasonable reply) is intentionally isolated
in `swm/judge.py` so it can be swapped from regex to a Claude API call without
touching the orchestrator.

## Storage layout

Append-only JSONL, organized per PR for easy auditing and garbage collection:

```
state/
└── <owner>/<repo>/
    └── pr-<N>/
        ├── polls.jsonl              # one line per poll cycle
        └── threads/
            └── <thread_id>.jsonl    # one line per poll for each Codex thread
```

Old records are NEVER overwritten. `cat polls.jsonl | jq` is a perfectly fine
ad-hoc query interface; for structured access see `swm.state.StateStore`.

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — agent-facing operating contract: permission
  stages, hard safety rules, output format, notification policy.
- [`rules/SWM-1100`](rules/SWM-1100-SOP-PR-Watch-Poll-Cycle.md) — canonical
  poll cycle steps (which `gh` commands fire, when to short-circuit).
- [`rules/SWM-1101`](rules/SWM-1101-SOP-Codex-Resolution-Verification.md) —
  Codex thread state classification, decision tree, substantive-reply
  heuristics, Stage 1.5 sync integration.
- [`rules/SWM-1102`](rules/SWM-1102-REF-Severity-Promotion-Rules.md) —
  context-aware severity demotion / promotion table.

## Development

```bash
uv pip install -e ".[dev]"
.venv/bin/pytest              # 99 tests (94 unit + 5 BDD), ~94% line coverage
```

The 80% coverage gate is enforced via `pyproject.toml` `--cov-fail-under=80`.

## License

MIT — see [LICENSE](LICENSE).

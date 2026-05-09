# CHG-1113: Clearance webhook receiver + auto-approve path

**Applies to:** SWM project
**Last updated:** 2026-05-09
**Last reviewed:** 2026-05-09
**Status:** Implemented
**Date:** 2026-05-09
**Requested by:** Frank Xu
**Priority:** P1 — operational path
**Change Type:** Normal

---

## What

Implement the event-driven Clearance flow:

```text
GitHub event
  -> Clearance GitHub App webhook
  -> Cloudflare Tunnel: https://gh.iterwheel.com/github/webhook
  -> Wukong/Mac mini local receiver
  -> swm poll repo --sync --actor clearance
  -> if READY: Clearance auto-approves
  -> Frank only merges
```

New surfaces:

- `swm/github_app.py`: builds a GitHub App JWT with RS256 via local `openssl`,
  exchanges it for an installation access token, and caches fresh tokens.
- `swm/webhook.py`: validates GitHub webhook HMAC, dedupes delivery IDs, maps
  payloads to allow-listed watched repos, runs `poll(..., sync=True)`, and
  submits approve reviews through the Clearance installation token when
  `auto_approve = true`.
- `swm webhook serve --config ~/.config/swm/watchd.toml`: local HTTP receiver
  intended to sit behind Cloudflare Tunnel.

No merge implementation is added. `auto_merge = true` is treated as a config
error for the affected watch.

## Config Shape

```toml
state_dir = "/Users/frank/Projects/sweeping-monk/state"

[server]
host = "127.0.0.1"
port = 8787
path = "/github/webhook"
webhook_secret_env = "SWM_WEBHOOK_SECRET"

[actors.clearance]
app_id = 123456
installation_id = 78901234
private_key_path = "/Users/frank/.config/swm/apps/iterwheel-clearance.private-key.pem"
bot_login = "iterwheel-clearance[bot]"

[[watch]]
repo = "frankyxhl/alfred"
base = "main"
actor = "clearance"
auto_resolve = true
auto_approve = true
auto_merge = false
```

## Safety

- Every webhook must pass `X-Hub-Signature-256` verification using
  `SWM_WEBHOOK_SECRET`.
- `X-GitHub-Delivery` is persisted to `state/webhook-deliveries.jsonl` to avoid
  duplicate approve attempts on redelivery.
- The receiver processes only `[[watch]]` allow-listed repos.
- Auto-approve reuses the existing SWM-1103 gates:
  - latest local poll must be `ready`;
  - current head SHA must match the poll head SHA;
  - actor must not be the PR author;
  - ledger is appended only after GitHub accepts the review.
- Approval uses the GitHub App installation token, so the review is attributed
  to Clearance. Tokens are never logged or written to JSONL.
- The approval call uses the REST pull-request review endpoint with
  `event=APPROVE` and `commit_id=<current head>` to bind the review to the
  verified head.

## Out of Scope

- Merging PRs.
- Web UI.
- AI/LLM judgment for ambiguous `NEEDS_HUMAN_JUDGMENT` cases. That remains the
  SWM-1111/SWM-1108 adapter/inbox path.
- Public tunnel provisioning. This CHG assumes Cloudflare Tunnel maps
  `https://gh.iterwheel.com` to `http://127.0.0.1:8787`.

## Acceptance

- [x] `swm webhook serve --config ...` command exists.
- [x] GitHub webhook signature verification is unit-tested.
- [x] Delivery ID dedupe is unit-tested.
- [x] A watched repo event runs poll with `sync=True`.
- [x] A READY PR is approved as the configured Clearance bot actor and ledgered.
- [x] `pytest` coverage gate passes.

---

## Change History

| Date | Change | By |
|------|--------|----|
| 2026-05-09 | Initial implementation: standard-library webhook receiver, GitHub App token broker, Clearance auto-approve path, tests, README docs | Codex |

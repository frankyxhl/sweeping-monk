"""GitHub webhook receiver for Clearance auto-resolve / auto-approve."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import tomllib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping

from . import guarded
from .gh import GhClient, GhCommandError
from .github_app import InstallationTokenProvider
from .models import LedgerAction, PollRecord, Status
from .poll import poll as run_poll
from .state import DEFAULT_STATE_DIR, StateStore, now_utc

RELEVANT_EVENTS = {
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_review_thread",
    "check_run",
    "check_suite",
    "status",
    "issue_comment",
}

# Per-(repo, pr, head_sha) locks prevent concurrent webhook deliveries for the
# same ready PR from both passing _already_approved_this_head() and submitting
# duplicate approvals under ThreadingHTTPServer.
_APPROVE_LOCKS: dict[str, threading.Lock] = {}
_APPROVE_LOCKS_MUTEX = threading.Lock()

# Reserve delivery IDs atomically before processing starts so a duplicate
# delivery that arrives while the first handler thread is still running
# (and hasn't written to the JSONL file yet) is still deduplicated.
_INFLIGHT_DELIVERIES: set[str] = set()
_INFLIGHT_MUTEX = threading.Lock()


def _approval_lock(repo: str, pr: int, head_sha: str) -> threading.Lock:
    key = f"{repo}/{pr}/{head_sha}"
    with _APPROVE_LOCKS_MUTEX:
        if key not in _APPROVE_LOCKS:
            _APPROVE_LOCKS[key] = threading.Lock()
        return _APPROVE_LOCKS[key]


@dataclass(frozen=True)
class ActorConfig:
    name: str
    app_id: int
    installation_id: int
    private_key_path: Path
    bot_login: str
    api_url: str = "https://api.github.com"


@dataclass(frozen=True)
class WatchConfig:
    repo: str
    base: str = "main"
    actor: str = "clearance"
    auto_resolve: bool = True
    auto_approve: bool = False
    auto_merge: bool = False


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    path: str = "/github/webhook"
    webhook_secret_env: str = "SWM_WEBHOOK_SECRET"


@dataclass(frozen=True)
class WebhookConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    state_dir: Path = DEFAULT_STATE_DIR
    actors: dict[str, ActorConfig] = field(default_factory=dict)
    watches: list[WatchConfig] = field(default_factory=list)

    def actor(self, name: str) -> ActorConfig:
        try:
            return self.actors[name]
        except KeyError as exc:
            raise ValueError(f"watch references unknown actor {name!r}") from exc

    def watches_for_repo(self, repo: str) -> list[WatchConfig]:
        return [w for w in self.watches if w.repo == repo]


@dataclass(frozen=True)
class WebhookAction:
    repo: str
    pr: int | None
    action: str
    detail: str


@dataclass(frozen=True)
class WebhookResult:
    delivery_id: str
    event: str
    repo: str | None
    status: str
    actions: list[WebhookAction] = field(default_factory=list)


def load_config(path: str | Path) -> WebhookConfig:
    config_path = Path(path).expanduser()
    with config_path.open("rb") as f:
        raw = tomllib.load(f)

    server_raw = raw.get("server") or {}
    server = ServerConfig(
        host=server_raw.get("host", "127.0.0.1"),
        port=int(server_raw.get("port", 8787)),
        path=server_raw.get("path", "/github/webhook"),
        webhook_secret_env=server_raw.get("webhook_secret_env", "SWM_WEBHOOK_SECRET"),
    )
    state_dir = Path(raw.get("state_dir", str(DEFAULT_STATE_DIR))).expanduser()

    actor_tables = raw.get("actors") or raw.get("actor") or {}
    actors: dict[str, ActorConfig] = {}
    for name, actor_raw in actor_tables.items():
        actors[name] = ActorConfig(
            name=name,
            app_id=int(actor_raw["app_id"]),
            installation_id=int(actor_raw["installation_id"]),
            private_key_path=Path(actor_raw["private_key_path"]).expanduser(),
            bot_login=actor_raw.get("bot_login", f"iterwheel-{name}[bot]"),
            api_url=actor_raw.get("api_url", "https://api.github.com"),
        )

    watches = [
        WatchConfig(
            repo=w["repo"],
            base=w.get("base", "main"),
            actor=w.get("actor", "clearance"),
            auto_resolve=bool(w.get("auto_resolve", True)),
            auto_approve=bool(w.get("auto_approve", False)),
            auto_merge=bool(w.get("auto_merge", False)),
        )
        for w in raw.get("watch", [])
    ]
    return WebhookConfig(server=server, state_dir=state_dir, actors=actors, watches=watches)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def verify_signature(*, body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, f"sha256={digest}")


def _delivery_path(store: StateStore) -> Path:
    return store.directory / "webhook-deliveries.jsonl"


def _delivery_seen(store: StateStore, delivery_id: str) -> bool:
    path = _delivery_path(store)
    if not path.exists():
        return False
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            try:
                if json.loads(line).get("delivery_id") == delivery_id:
                    return True
            except json.JSONDecodeError:
                continue
    return False


def _append_delivery(store: StateStore, result: WebhookResult) -> None:
    path = _delivery_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": now_utc().isoformat(),
        "delivery_id": result.delivery_id,
        "event": result.event,
        "repo": result.repo,
        "status": result.status,
        "actions": [a.__dict__ for a in result.actions],
    }
    with path.open("a") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def _repo_from_payload(payload: dict) -> str | None:
    repo = payload.get("repository") or {}
    full_name = repo.get("full_name")
    return str(full_name) if full_name else None


def _already_approved_this_head(
    store: StateStore, *, repo: str, pr: int, head_sha: str, actor: str,
) -> bool:
    for entry in store.read_ledger(repo, pr):
        action = entry.action.value if hasattr(entry.action, "value") else str(entry.action)
        if (
            action == LedgerAction.SUBMIT_REVIEW_APPROVE.value
            and entry.head_sha == head_sha
            and entry.actor == actor
        ):
            return True
    return False


def _auto_approve_ready_pr(
    *,
    store: StateStore,
    gh_client: GhClient,
    record: PollRecord,
    actor_label: str,
) -> WebhookAction:
    if record.status is not Status.READY:
        return WebhookAction(record.repo, record.pr, "skip-approve", f"status={record.status.value}")

    try:
        view = gh_client.view_pr(record.repo, record.pr, ["headRefOid", "author"])
        current_head = view.get("headRefOid", "")
        if not current_head:
            return WebhookAction(record.repo, record.pr, "approve-blocked", "could not read current head")
        if current_head != record.head_sha:
            return WebhookAction(
                record.repo, record.pr, "approve-blocked",
                f"head drifted since poll ({record.head_sha[:8]} → {current_head[:8]})",
            )
    except GhCommandError as exc:
        return WebhookAction(record.repo, record.pr, "approve-error", str(exc))

    with _approval_lock(record.repo, record.pr, current_head):
        try:
            if _already_approved_this_head(
                store, repo=record.repo, pr=record.pr, head_sha=current_head,
                actor=gh_client.auth_active_login(),
            ):
                return WebhookAction(record.repo, record.pr, "skip-approve", "already approved this head")

            identity = guarded.check_identity(gh_client, record.repo, record.pr)
            verdict = guarded.check_verdict(store, record.repo, record.pr, current_head)
            blockers: list[str] = []
            if identity.blocker:
                blockers.append(identity.blocker)
            ok, why = verdict.supports_approve()
            if not ok and why:
                blockers.append(why)
            if blockers:
                return WebhookAction(record.repo, record.pr, "approve-blocked", "; ".join(blockers))

            # Re-read the head inside the lock, right before submitting, to
            # catch drift that occurred during identity/verdict checks above.
            live_view = gh_client.view_pr(record.repo, record.pr, ["headRefOid"])
            live_head = live_view.get("headRefOid", "")
            if live_head != record.head_sha:
                return WebhookAction(
                    record.repo, record.pr, "approve-blocked",
                    f"head drifted before approval ({record.head_sha[:8]} → {live_head[:8]})",
                )

            reason = f"clearance-auto-approve: swm ready @ {current_head[:8]}"
            body = guarded.render_approve_body(record, reason, actor_label=actor_label)
            review_result = gh_client.submit_review_approve(
                record.repo, record.pr, body, commit_id=current_head,
            )
        except GhCommandError as exc:
            return WebhookAction(record.repo, record.pr, "approve-error", str(exc))

        # Ledger immediately after the write succeeds — before nonessential verification,
        # so a transient verify failure cannot leave an unaudited approval.
        entry = guarded.build_approve_ledger_entry(
            poll=record,
            actor=identity.active_login,
            reason=reason,
            authorized_by="standing authorization (webhook auto_approve=true)",
            review_result={"stdout": review_result.get("stdout", "")},
        )
        store.append_ledger(entry)

    try:
        verify = gh_client.view_pr(record.repo, record.pr, ["reviewDecision", "mergeStateStatus"])
    except GhCommandError:
        verify = {}
    return WebhookAction(
        record.repo,
        record.pr,
        "approved",
        f"reviewDecision={verify.get('reviewDecision')} mergeStateStatus={verify.get('mergeStateStatus')}",
    )


def process_webhook(
    *,
    headers: Mapping[str, str],
    body: bytes,
    config: WebhookConfig,
    token_provider: InstallationTokenProvider | None = None,
    store: StateStore | None = None,
) -> WebhookResult:
    secret = os.environ.get(config.server.webhook_secret_env)
    if not secret:
        raise ValueError(f"{config.server.webhook_secret_env} is not set")
    if not verify_signature(body=body, signature=_header(headers, "X-Hub-Signature-256"), secret=secret):
        raise PermissionError("invalid GitHub webhook signature")

    delivery_id = _header(headers, "X-GitHub-Delivery") or ""
    event = _header(headers, "X-GitHub-Event") or ""
    if not delivery_id:
        raise ValueError("missing X-GitHub-Delivery")
    if event not in RELEVANT_EVENTS:
        return WebhookResult(delivery_id, event, None, "ignored-event")

    payload = json.loads(body.decode("utf-8"))
    repo = _repo_from_payload(payload)
    if not repo:
        return WebhookResult(delivery_id, event, None, "ignored-no-repo")

    store = store or StateStore(config.state_dir)

    with _INFLIGHT_MUTEX:
        if delivery_id in _INFLIGHT_DELIVERIES or _delivery_seen(store, delivery_id):
            return WebhookResult(delivery_id, event, repo, "duplicate")
        _INFLIGHT_DELIVERIES.add(delivery_id)

    try:
        watches = config.watches_for_repo(repo)
        if not watches:
            result = WebhookResult(delivery_id, event, repo, "ignored-repo")
            _append_delivery(store, result)
            return result

        token_provider = token_provider or InstallationTokenProvider()
        actions: list[WebhookAction] = []
        for watch in watches:
            if watch.auto_merge:
                actions.append(WebhookAction(watch.repo, None, "config-error", "auto_merge is forbidden"))
                continue
            actor = config.actor(watch.actor)
            token = token_provider.token_for(
                app_id=actor.app_id,
                installation_id=actor.installation_id,
                private_key_path=actor.private_key_path,
                api_url=actor.api_url,
            )
            gh_client = GhClient(token=token, actor_login=actor.bot_login)
            try:
                outcomes = run_poll(
                    watch.repo,
                    store=store,
                    gh_client=gh_client,
                    sync=watch.auto_resolve,
                    base=watch.base,
                )
            except Exception as exc:
                # A sync failure mid-resolve would leave partial mutations
                # unaudited if the delivery is not recorded. Capture the error
                # and fall through to _append_delivery so GitHub does not
                # redeliver and double-mutate already-resolved threads.
                actions.append(WebhookAction(watch.repo, None, "poll-error", str(exc)))
                continue
            if not outcomes:
                actions.append(WebhookAction(watch.repo, None, "poll", "no open PRs"))
            for outcome in outcomes:
                if watch.auto_approve:
                    actions.append(_auto_approve_ready_pr(
                        store=store,
                        gh_client=gh_client,
                        record=outcome.record,
                        actor_label="Iterwheel Clearance automation",
                    ))
                elif outcome.sync_actions:
                    actions.append(WebhookAction(
                        outcome.record.repo,
                        outcome.record.pr,
                        "resolved",
                        f"{len(outcome.sync_actions)} thread(s)",
                    ))
                else:
                    actions.append(WebhookAction(
                        outcome.record.repo,
                        outcome.record.pr,
                        "polled",
                        f"status={outcome.record.status.value}",
                    ))

        result = WebhookResult(delivery_id, event, repo, "processed", actions)
        _append_delivery(store, result)
        return result
    finally:
        with _INFLIGHT_MUTEX:
            _INFLIGHT_DELIVERIES.discard(delivery_id)


def serve(config: WebhookConfig) -> None:
    token_provider = InstallationTokenProvider()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
            if self.path != config.server.path:
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            try:
                result = process_webhook(
                    headers=self.headers, body=body, config=config,
                    token_provider=token_provider,
                )
                response = json.dumps({
                    "status": result.status,
                    "repo": result.repo,
                    "actions": [a.__dict__ for a in result.actions],
                }).encode("utf-8")
                self.send_response(200)
            except PermissionError as exc:
                response = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(401)
            except Exception as exc:  # pragma: no cover - exercised manually
                response = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    httpd = ThreadingHTTPServer((config.server.host, config.server.port), Handler)
    httpd.serve_forever()

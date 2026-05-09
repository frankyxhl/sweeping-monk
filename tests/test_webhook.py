from __future__ import annotations

import hmac
import hashlib
import json
from pathlib import Path

import pytest

from swm.models import CIConclusion, PollRecord, Status
from swm.poll import PollOutcome
from swm.state import StateStore
from swm.webhook import (
    ActorConfig,
    ServerConfig,
    WatchConfig,
    WebhookConfig,
    load_config,
    process_webhook,
    verify_signature,
)


def _signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _headers(body: bytes, *, delivery: str = "d1", event: str = "pull_request") -> dict[str, str]:
    return {
        "X-Hub-Signature-256": _signature(body, "secret"),
        "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": event,
    }


def _config(tmp_path: Path) -> WebhookConfig:
    return WebhookConfig(
        server=ServerConfig(webhook_secret_env="SWM_TEST_WEBHOOK_SECRET"),
        state_dir=tmp_path / "state",
        actors={
            "clearance": ActorConfig(
                name="clearance",
                app_id=123,
                installation_id=456,
                private_key_path=tmp_path / "clearance.pem",
                bot_login="iterwheel-clearance[bot]",
            ),
        },
        watches=[
            WatchConfig(
                repo="owner/repo",
                base="main",
                actor="clearance",
                auto_resolve=True,
                auto_approve=True,
            ),
        ],
    )


def test_verify_signature_accepts_sha256_hmac() -> None:
    body = b'{"ok":true}'
    assert verify_signature(body=body, signature=_signature(body, "secret"), secret="secret")
    assert not verify_signature(body=body, signature=_signature(body, "wrong"), secret="secret")


def test_load_config_supports_clearance_actor_table(tmp_path: Path) -> None:
    cfg_path = tmp_path / "watchd.toml"
    cfg_path.write_text(
        """
state_dir = "/tmp/swm-state"

[server]
host = "127.0.0.1"
port = 8787
path = "/github/webhook"
webhook_secret_env = "SWM_WEBHOOK_SECRET"

[actors.clearance]
app_id = 123
installation_id = 456
private_key_path = "~/clearance.pem"
bot_login = "iterwheel-clearance[bot]"

[[watch]]
repo = "owner/repo"
base = "main"
actor = "clearance"
auto_resolve = true
auto_approve = true
auto_merge = false
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)

    assert cfg.server.path == "/github/webhook"
    assert cfg.actor("clearance").app_id == 123
    assert cfg.watches[0].repo == "owner/repo"
    assert cfg.watches[0].auto_approve is True


def test_process_webhook_polls_and_auto_approves_ready_pr(
    tmp_path: Path, monkeypatch
) -> None:
    body = json.dumps({"repository": {"full_name": "owner/repo"}}).encode()
    store = StateStore(tmp_path / "state")
    cfg = _config(tmp_path)
    monkeypatch.setenv("SWM_TEST_WEBHOOK_SECRET", "secret")

    head = "abc12345" + "0" * 32
    record = PollRecord(
        ts=__import__("datetime").datetime(2026, 5, 9, 10, 0, tzinfo=__import__("datetime").timezone.utc),
        repo="owner/repo",
        pr=66,
        title="ready PR",
        head_sha=head,
        status=Status.READY,
        ci={"test": CIConclusion.SUCCESS},
        merge_state="CLEAN",
        codex_open=0,
        codex_resolved=0,
        threads=[],
        trigger="webhook-test",
    )
    poll_calls = []

    def fake_run_poll(repo, *, store, gh_client, sync, base):
        poll_calls.append((repo, sync, base))
        store.append_poll(record)
        return [PollOutcome(record=record, snapshots=[], sync_actions=[], is_no_change=False)]

    class FakeTokenProvider:
        def token_for(self, **kwargs):
            return "installation-token"

    from tests.conftest import FakeGhClient

    class ClearanceGh(FakeGhClient):
        def __init__(self, *, token=None, actor_login=None):
            super().__init__(
                prs=[{
                    "number": 66,
                    "headRefOid": head,
                    "author": {"login": "someone"},
                    "reviewDecision": "APPROVED",
                    "mergeStateStatus": "CLEAN",
                }],
                active_login=actor_login or "iterwheel-clearance[bot]",
            )
            self.token = token

    monkeypatch.setattr("swm.webhook.run_poll", fake_run_poll)
    monkeypatch.setattr("swm.webhook.GhClient", ClearanceGh)

    result = process_webhook(
        headers=_headers(body),
        body=body,
        config=cfg,
        token_provider=FakeTokenProvider(),
        store=store,
    )

    assert result.status == "processed"
    assert poll_calls == [("owner/repo", True, "main")]
    assert any(a.action == "approved" for a in result.actions)
    ledger = store.read_ledger("owner/repo", 66)
    assert len(ledger) == 1
    assert ledger[0].actor == "iterwheel-clearance[bot]"
    assert ledger[0].authorized_by == "standing authorization (webhook auto_approve=true)"


def test_process_webhook_rejects_bad_signature(tmp_path: Path, monkeypatch) -> None:
    body = b'{"repository":{"full_name":"owner/repo"}}'
    monkeypatch.setenv("SWM_TEST_WEBHOOK_SECRET", "secret")

    with pytest.raises(PermissionError):
        process_webhook(
            headers={
                "X-Hub-Signature-256": _signature(body, "wrong"),
                "X-GitHub-Delivery": "d1",
                "X-GitHub-Event": "pull_request",
            },
            body=body,
            config=_config(tmp_path),
            store=StateStore(tmp_path / "state"),
        )


def test_process_webhook_dedupes_delivery_id(tmp_path: Path, monkeypatch) -> None:
    body = json.dumps({"repository": {"full_name": "other/repo"}}).encode()
    store = StateStore(tmp_path / "state")
    cfg = _config(tmp_path)
    monkeypatch.setenv("SWM_TEST_WEBHOOK_SECRET", "secret")

    first = process_webhook(headers=_headers(body), body=body, config=cfg, store=store)
    second = process_webhook(headers=_headers(body), body=body, config=cfg, store=store)

    assert first.status == "ignored-repo"
    assert second.status == "duplicate"


def test_auto_approve_ledgers_even_when_verify_view_pr_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """Ledger entry must be written immediately after submit_review_approve succeeds,
    not after the nonessential verification view_pr — so a transient verify error
    cannot leave an unaudited approval."""
    import datetime as _dt
    from swm.gh import GhCommandError
    from swm.models import CIConclusion, PollRecord, Status
    from swm.poll import PollOutcome
    from swm.state import StateStore
    from tests.conftest import FakeGhClient

    store = StateStore(tmp_path / "state")
    cfg = _config(tmp_path)
    monkeypatch.setenv("SWM_TEST_WEBHOOK_SECRET", "secret")

    head = "verify00" + "0" * 32
    record = PollRecord(
        ts=_dt.datetime(2026, 5, 9, 10, 0, tzinfo=_dt.timezone.utc),
        repo="owner/repo", pr=66, title="verify-fail PR",
        head_sha=head, status=Status.READY,
        ci={"test": CIConclusion.SUCCESS}, merge_state="CLEAN",
        codex_open=0, codex_resolved=0, threads=[], trigger="test",
    )

    class VerifyFailGh(FakeGhClient):
        def __init__(self, *, token=None, actor_login=None):
            super().__init__(
                prs=[{"number": 66, "headRefOid": head, "author": {"login": "someone"}}],
                active_login=actor_login or "iterwheel-clearance[bot]",
            )
            self.token = token

        def view_pr(self, repo, pr, fields):
            self._record("view_pr", repo, pr, fields=fields)
            # Pre-approve reads succeed; the post-approve verify raises.
            if "reviewDecision" in fields:
                raise GhCommandError("transient verify failure")
            return next(p for p in self._prs if p["number"] == pr)

    def fake_run_poll(repo, *, store, gh_client, sync, base):
        store.append_poll(record)
        return [PollOutcome(record=record, snapshots=[], sync_actions=[], is_no_change=False)]

    class FakeTokenProvider:
        def token_for(self, **kwargs):
            return "installation-token"

    monkeypatch.setattr("swm.webhook.run_poll", fake_run_poll)
    monkeypatch.setattr("swm.webhook.GhClient", VerifyFailGh)

    body = json.dumps({"repository": {"full_name": "owner/repo"}}).encode()
    result = process_webhook(
        headers=_headers(body), body=body, config=cfg,
        token_provider=FakeTokenProvider(), store=store,
    )

    # Approval must still be recorded as "approved" (not "approve-error").
    assert any(a.action == "approved" for a in result.actions), \
        f"expected approved action, got: {[a.action for a in result.actions]}"
    # Ledger must exist despite verify failure.
    ledger = store.read_ledger("owner/repo", 66)
    assert len(ledger) == 1, "ledger entry must survive a verify view_pr failure"
    assert ledger[0].actor == "iterwheel-clearance[bot]"


def test_auto_approve_blocks_when_head_drifts_between_poll_and_view(
    tmp_path: Path, monkeypatch
) -> None:
    """If the PR head advances after run_poll() but before view_pr() in
    _auto_approve_ready_pr, approval must be blocked — not submitted against
    the stale record."""
    import datetime as _dt
    from swm.state import StateStore
    from tests.conftest import FakeGhClient

    store = StateStore(tmp_path / "state")
    cfg = _config(tmp_path)
    monkeypatch.setenv("SWM_TEST_WEBHOOK_SECRET", "secret")

    poll_head = "pollhead0" + "0" * 31
    live_head = "livehead0" + "0" * 31  # head advanced since poll

    record = PollRecord(
        ts=_dt.datetime(2026, 5, 9, 11, 0, tzinfo=_dt.timezone.utc),
        repo="owner/repo", pr=77, title="drift PR",
        head_sha=poll_head, status=Status.READY,
        ci={"test": CIConclusion.SUCCESS}, merge_state="CLEAN",
        codex_open=0, codex_resolved=0, threads=[], trigger="test",
    )

    class DriftGh(FakeGhClient):
        def __init__(self, *, token=None, actor_login=None):
            super().__init__(
                prs=[{"number": 77, "headRefOid": live_head, "author": {"login": "someone"}}],
                active_login=actor_login or "iterwheel-clearance[bot]",
            )
            self.token = token

    def fake_run_poll(repo, *, store, gh_client, sync, base):
        store.append_poll(record)
        return [PollOutcome(record=record, snapshots=[], sync_actions=[], is_no_change=False)]

    class FakeTokenProvider:
        def token_for(self, **kwargs):
            return "installation-token"

    monkeypatch.setattr("swm.webhook.run_poll", fake_run_poll)
    monkeypatch.setattr("swm.webhook.GhClient", DriftGh)

    body = json.dumps({"repository": {"full_name": "owner/repo"}}).encode()
    result = process_webhook(
        headers=_headers(body, delivery="d-drift"), body=body, config=cfg,
        token_provider=FakeTokenProvider(), store=store,
    )

    actions = result.actions
    assert any(a.action == "approve-blocked" and "drifted" in a.detail for a in actions), \
        f"expected approve-blocked with 'drifted', got: {[(a.action, a.detail) for a in actions]}"
    assert store.read_ledger("owner/repo", 77) == []


def test_auto_approve_serializes_concurrent_calls_for_same_head(
    tmp_path: Path, monkeypatch
) -> None:
    """Two concurrent webhook deliveries for the same ready PR+head must submit
    exactly one approval — the per-head lock must serialize them."""
    import threading
    import datetime as _dt
    from swm.webhook import _auto_approve_ready_pr
    from swm.state import StateStore
    from tests.conftest import FakeGhClient

    store = StateStore(tmp_path / "state")
    head = "concurr00" + "0" * 31

    record = PollRecord(
        ts=_dt.datetime(2026, 5, 9, 13, 0, tzinfo=_dt.timezone.utc),
        repo="owner/repo", pr=88, title="concurrent PR",
        head_sha=head, status=Status.READY,
        ci={"test": CIConclusion.SUCCESS}, merge_state="CLEAN",
        codex_open=0, codex_resolved=0, threads=[], trigger="test",
    )

    # Pre-populate the store so check_verdict finds the poll record.
    store.append_poll(record)

    approve_calls: list[int] = []
    # Barrier synchronizes both threads right before the per-head lock is
    # acquired, guaranteeing the race condition is exercised.
    barrier = threading.Barrier(2)

    class SyncGh(FakeGhClient):
        def __init__(self, **kwargs):
            super().__init__(
                prs=[{"number": 88, "headRefOid": head, "author": {"login": "alice"}}],
                active_login="iterwheel-clearance[bot]",
            )

        def view_pr(self, repo, pr, fields):
            # Only barrier on the initial head-read call (before the lock).
            # check_identity's view_pr(["author"]) call is inside the lock and
            # must not hit the barrier to avoid deadlock.
            if set(fields) == {"headRefOid", "author"}:
                barrier.wait()
            return {"headRefOid": head, "author": {"login": "alice"}}

        def submit_review_approve(self, repo, pr, body, *, commit_id):
            approve_calls.append(1)
            return {"stdout": ""}

    results: list = [None, None]

    def worker(idx: int) -> None:
        results[idx] = _auto_approve_ready_pr(
            store=store, gh_client=SyncGh(), record=record, actor_label="test",
        )

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(approve_calls) == 1, \
        f"submit_review_approve must be called exactly once, got {len(approve_calls)}"
    actions = [r.action for r in results if r is not None]
    assert "approved" in actions
    assert "skip-approve" in actions

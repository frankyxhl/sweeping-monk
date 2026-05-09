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

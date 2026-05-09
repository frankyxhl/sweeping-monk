from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from swm.github_app import InstallationToken, InstallationTokenProvider, build_app_jwt, fetch_installation_token


def _decode_part(part: str) -> dict:
    padded = part + "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()).decode())


def test_build_app_jwt_shapes_rs256_claims() -> None:
    token = build_app_jwt(
        app_id=12345,
        private_key_path=Path("/tmp/key.pem"),
        now=1_700_000_000,
        signer=lambda msg, path: b"sig",
    )
    header, payload, signature = token.split(".")

    assert _decode_part(header) == {"alg": "RS256", "typ": "JWT"}
    claims = _decode_part(payload)
    assert claims["iss"] == "12345"
    assert claims["iat"] == 1_699_999_940
    assert claims["exp"] == 1_700_000_480
    assert signature == "c2ln"


def test_fetch_installation_token_posts_with_bearer_jwt() -> None:
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"token":"iat-token","expires_at":"2026-05-09T10:00:00Z"}'

    def opener(req, *, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.headers["Authorization"]
        seen["timeout"] = timeout
        return Response()

    token = fetch_installation_token(
        app_id=1,
        installation_id=2,
        private_key_path=Path("/tmp/key.pem"),
        opener=opener,
        signer=lambda msg, path: b"sig",
    )

    assert seen["url"] == "https://api.github.com/app/installations/2/access_tokens"
    assert seen["auth"].startswith("Bearer ")
    assert seen["timeout"] == 10
    assert token.token == "iat-token"
    assert token.expires_at == datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)


def test_installation_token_fresh_uses_five_minute_margin() -> None:
    token = InstallationToken(
        token="x",
        expires_at=datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc),
    )
    assert token.fresh(now=datetime(2026, 5, 9, 9, 54, tzinfo=timezone.utc))
    assert not token.fresh(now=datetime(2026, 5, 9, 9, 56, tzinfo=timezone.utc))


def test_installation_token_provider_reuses_fresh_cache(monkeypatch) -> None:
    calls = []

    def fake_fetch(**kwargs):
        calls.append(kwargs)
        return InstallationToken(
            token=f"token-{len(calls)}",
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )

    import swm.github_app as github_app
    monkeypatch.setattr(github_app, "fetch_installation_token", fake_fetch)

    provider = InstallationTokenProvider()
    kwargs = {
        "app_id": 1,
        "installation_id": 2,
        "private_key_path": Path("/tmp/key.pem"),
    }
    assert provider.token_for(**kwargs) == "token-1"
    assert provider.token_for(**kwargs) == "token-1"
    assert len(calls) == 1

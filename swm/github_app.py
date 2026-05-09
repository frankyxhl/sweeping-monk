"""GitHub App installation-token support for Clearance-style actors."""
from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


Signer = Callable[[bytes, Path], bytes]
UrlOpen = Callable[..., object]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _openssl_sign_rs256(message: bytes, private_key_path: Path) -> bytes:
    proc = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(private_key_path)],
        input=message,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"openssl failed to sign GitHub App JWT: {proc.stderr.decode().strip()}")
    return proc.stdout


def build_app_jwt(
    *,
    app_id: int,
    private_key_path: Path,
    now: int | None = None,
    signer: Signer = _openssl_sign_rs256,
) -> str:
    """Build a GitHub App JWT using RS256.

    `now` and `signer` are injectable so tests do not need a real private key.
    """
    issued_at = int(now if now is not None else time.time()) - 60
    payload = {
        "iat": issued_at,
        "exp": issued_at + 9 * 60,
        "iss": str(app_id),
    }
    header = {"alg": "RS256", "typ": "JWT"}
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    ).encode("ascii")
    signature = signer(signing_input, private_key_path)
    return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: datetime

    def fresh(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return current + timedelta(minutes=5) < self.expires_at


def fetch_installation_token(
    *,
    app_id: int,
    installation_id: int,
    private_key_path: Path,
    api_url: str = "https://api.github.com",
    opener: UrlOpen = urllib.request.urlopen,
    signer: Signer = _openssl_sign_rs256,
) -> InstallationToken:
    jwt = build_app_jwt(
        app_id=app_id,
        private_key_path=private_key_path,
        signer=signer,
    )
    url = f"{api_url.rstrip('/')}/app/installations/{installation_id}/access_tokens"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {jwt}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sweeping-monk",
        },
    )
    with opener(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return InstallationToken(
        token=data["token"],
        expires_at=datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")),
    )


class InstallationTokenProvider:
    """Small in-memory cache; installation tokens expire after about an hour."""

    def __init__(self, *, opener: UrlOpen = urllib.request.urlopen) -> None:
        self._opener = opener
        self._cache: dict[tuple[int, int, Path], InstallationToken] = {}

    def token_for(
        self,
        *,
        app_id: int,
        installation_id: int,
        private_key_path: Path,
        api_url: str = "https://api.github.com",
    ) -> str:
        key = (app_id, installation_id, private_key_path)
        cached = self._cache.get(key)
        if cached and cached.fresh():
            return cached.token
        fetched = fetch_installation_token(
            app_id=app_id,
            installation_id=installation_id,
            private_key_path=private_key_path,
            api_url=api_url,
            opener=self._opener,
        )
        self._cache[key] = fetched
        return fetched.token

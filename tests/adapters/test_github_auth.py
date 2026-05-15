"""GitHubAppAuth — JWT minting + installation token cache/refresh.

Covers:
  - constructor validation (positive id, PEM-looking key)
  - app_jwt: RS256-signed, iat/exp/iss claims correct
  - installation_token: fetched once, cached, refreshed near expiry
  - 401 / 5xx from /access_tokens propagates as HTTPStatusError
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from openbot.adapters.github_auth import GitHubAppAuth, InstallationToken

_APP_ID = 42_424_242


@pytest.fixture
async def auth_factory(rsa_private_key_pem: bytes) -> Any:
    """Builds GitHubAppAuth instances and aclose()s them all on teardown."""
    instances: list[GitHubAppAuth] = []

    def make(
        handler: Any | None = None,
        **kwargs: Any,
    ) -> GitHubAppAuth:
        http = (
            httpx.AsyncClient(transport=httpx.MockTransport(handler))
            if handler is not None
            else None
        )
        instance = GitHubAppAuth(
            app_id=_APP_ID,
            private_key_pem=rsa_private_key_pem,
            http=http,
            **kwargs,
        )
        instances.append(instance)
        return instance

    yield make
    for inst in instances:
        await inst.aclose()


def _public_key(pem: bytes) -> Any:
    private = serialization.load_pem_private_key(pem, password=None)
    return private.public_key()


def _expires(seconds_from_now: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ───── constructor ─────


def test_rejects_non_positive_app_id(rsa_private_key_pem: bytes) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        GitHubAppAuth(app_id=0, private_key_pem=rsa_private_key_pem)


def test_rejects_non_pem_key() -> None:
    with pytest.raises(ValueError, match="PEM"):
        GitHubAppAuth(app_id=_APP_ID, private_key_pem=b"not-a-pem")


# ───── app_jwt ─────


def test_app_jwt_is_rs256_with_correct_claims(rsa_private_key_pem: bytes) -> None:
    auth = GitHubAppAuth(app_id=_APP_ID, private_key_pem=rsa_private_key_pem)
    now = 1_700_000_000

    token = auth.app_jwt(now=now)

    # Decode without verifying expiry — we want to inspect claims at `now`.
    claims = jwt.decode(
        token,
        _public_key(rsa_private_key_pem),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert int(claims["iss"]) == _APP_ID  # serialized as string per PyJWT 2.10+
    assert claims["iat"] == now - 60  # 60s clock-skew backdate
    assert 0 < claims["exp"] - now <= 10 * 60  # within GitHub's 10min limit


def test_app_jwt_can_be_verified_by_public_key(rsa_private_key_pem: bytes) -> None:
    auth = GitHubAppAuth(app_id=_APP_ID, private_key_pem=rsa_private_key_pem)
    token = auth.app_jwt()
    # No exception ⇒ signature verifies and exp is in the future.
    jwt.decode(token, _public_key(rsa_private_key_pem), algorithms=["RS256"])


def test_app_jwt_rejects_tampering(rsa_private_key_pem: bytes) -> None:
    auth = GitHubAppAuth(app_id=_APP_ID, private_key_pem=rsa_private_key_pem)
    token = auth.app_jwt()
    tampered = token[:-4] + ("A" * 4)
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(tampered, _public_key(rsa_private_key_pem), algorithms=["RS256"])


# ───── installation_token ─────


async def test_installation_token_fetches_once_and_caches(auth_factory: Any) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            201, json={"token": "ghs_test_token_123", "expires_at": _expires(3600)}
        )

    auth = auth_factory(handler)

    first = await auth.installation_token(99)
    second = await auth.installation_token(99)

    assert first is second  # exact cache hit
    assert first.token == "ghs_test_token_123"
    assert first.installation_id == 99
    assert len(calls) == 1  # only one network call
    # Sanity: request was authenticated with a Bearer App JWT.
    assert calls[0].headers["authorization"].startswith("Bearer ")


async def test_installation_token_refreshes_when_stale(auth_factory: Any) -> None:
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        # First response expires almost immediately; second is fresh.
        expires = _expires(60) if counter["n"] == 1 else _expires(3600)
        return httpx.Response(201, json={"token": f"ghs_v{counter['n']}", "expires_at": expires})

    auth = auth_factory(handler)

    stale = await auth.installation_token(7)
    assert stale.token == "ghs_v1"
    # is_fresh uses a 5-min slack — 60s expiry counts as stale already.
    assert not stale.is_fresh()

    fresh = await auth.installation_token(7)
    assert fresh.token == "ghs_v2"
    assert fresh is not stale


async def test_installation_token_propagates_401(auth_factory: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    auth = auth_factory(handler)

    with pytest.raises(httpx.HTTPStatusError):
        await auth.installation_token(1)


async def test_installation_token_rejects_non_positive_id(auth_factory: Any) -> None:
    auth = auth_factory()
    with pytest.raises(ValueError, match="positive integer"):
        await auth.installation_token(0)


# ───── InstallationToken freshness ─────


def test_token_is_fresh_when_far_from_expiry() -> None:
    tok = InstallationToken(
        token="t", expires_at=datetime.now(UTC) + timedelta(hours=1), installation_id=1
    )
    assert tok.is_fresh()


def test_token_is_stale_within_refresh_slack() -> None:
    tok = InstallationToken(
        token="t", expires_at=datetime.now(UTC) + timedelta(minutes=1), installation_id=1
    )
    assert not tok.is_fresh()

"""Tenacity retry tests for GitHubAdapter._authed_json.

Two contracts the production code relies on:

  1. Transient gateway errors (502 / 503 / 504 / ConnectError / ReadTimeout)
     trigger up to ``_RETRY_MAX_ATTEMPTS - 1`` retries.
  2. Non-retryable errors (401 / 403 / 404 / 422) bubble up immediately —
     retrying a 404 just delays the inevitable.

We patch ``asyncio.sleep`` to a no-op so the exponential backoff doesn't
make the test suite take ~5s per retry case.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from openbot.adapters.github import (
    _RETRY_MAX_ATTEMPTS,
    GitHubAdapter,
    _is_retryable_github_error,
)
from openbot.adapters.github_auth import InstallationToken
from openbot.events import EventKind, UnifiedEvent

_SECRET = "test-secret"
_INSTALL_ID = 9_999_999
_INSTALL_TOKEN = "ghs_install_token_xyz"


class _FakeAuth:
    """Stand-in for GitHubAppAuth (avoids JWT signing in unit tests)."""

    async def installation_token(self, installation_id: int) -> InstallationToken:
        return InstallationToken(
            token=_INSTALL_TOKEN,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            installation_id=installation_id,
        )

    async def aclose(self) -> None:
        return None


def _event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="d1",
        kind=EventKind.ISSUE_OPENED,
        repo="YiAgent/openbot",
        actor="someone",
        issue_number=42,
        installation_id=_INSTALL_ID,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip tenacity's exponential backoff so retries are instant.

    Tenacity's AsyncRetrying uses ``asyncio.sleep`` between attempts;
    patching it keeps the test suite responsive without changing the
    retry-count contract under test.
    """
    import asyncio

    async def _instant(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


# ───── classifier ─────


def test_is_retryable_for_503_status_error() -> None:
    response = httpx.Response(503, request=httpx.Request("GET", "https://x"))
    err = httpx.HTTPStatusError("service unavailable", request=response.request, response=response)
    assert _is_retryable_github_error(err) is True


@pytest.mark.parametrize("status", [502, 504])
def test_is_retryable_for_other_gateway_statuses(status: int) -> None:
    response = httpx.Response(status, request=httpx.Request("GET", "https://x"))
    err = httpx.HTTPStatusError("gateway", request=response.request, response=response)
    assert _is_retryable_github_error(err) is True


@pytest.mark.parametrize("status", [401, 403, 404, 422, 500])
def test_is_NOT_retryable_for_contract_errors(status: int) -> None:
    """500 is intentionally NOT retried — GitHub returns 500 on persistent
    bugs in their side, retrying just doubles billing without changing the
    outcome. Reserve retries for the explicit "try again" codes (502/503/504)."""
    response = httpx.Response(status, request=httpx.Request("GET", "https://x"))
    err = httpx.HTTPStatusError("err", request=response.request, response=response)
    assert _is_retryable_github_error(err) is False


def test_is_retryable_for_connect_error() -> None:
    assert _is_retryable_github_error(httpx.ConnectError("nope")) is True


def test_is_retryable_for_read_timeout() -> None:
    assert _is_retryable_github_error(httpx.ReadTimeout("slow")) is True


def test_not_retryable_for_unrelated_exception() -> None:
    assert _is_retryable_github_error(ValueError("nope")) is False


# ───── decorator behavior ─────


def _make_handler(responses: list[httpx.Response]) -> Any:
    """Return a MockTransport handler that yields responses in order.

    Once exhausted, raises AssertionError — catches the case where the
    code under test retries more than expected.
    """
    iterator = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            return next(iterator)
        except StopIteration as exc:
            raise AssertionError("handler invoked beyond expected attempts") from exc

    return handler


async def test_retries_on_503_then_succeeds() -> None:
    """Two transient 503s + final 200 → request succeeds, 3 attempts total."""
    handler = _make_handler(
        [
            httpx.Response(503, json={"message": "down"}),
            httpx.Response(503, json={"message": "down"}),
            httpx.Response(201, json={"id": 1}),
        ]
    )
    adapter = GitHubAdapter(
        webhook_secret=_SECRET,
        auth=_FakeAuth(),  # type: ignore[arg-type]
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        result = await adapter.reply(_event(), "hi")
        assert result == {"id": 1}
    finally:
        await adapter.aclose()


async def test_retries_exhaust_then_raises() -> None:
    """Three 503s back-to-back → exhaust retry budget, original error bubbles."""
    handler = _make_handler([httpx.Response(503) for _ in range(_RETRY_MAX_ATTEMPTS)])
    adapter = GitHubAdapter(
        webhook_secret=_SECRET,
        auth=_FakeAuth(),  # type: ignore[arg-type]
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await adapter.reply(_event(), "hi")
        # reraise=True → callers see the real exception, not RetryError.
        assert excinfo.value.response.status_code == 503
    finally:
        await adapter.aclose()


async def test_does_not_retry_on_404() -> None:
    """404 is a contract failure; retrying just wastes API budget."""
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(404, json={"message": "Not Found"})

    adapter = GitHubAdapter(
        webhook_secret=_SECRET,
        auth=_FakeAuth(),  # type: ignore[arg-type]
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.reply(_event(), "hi")
    finally:
        await adapter.aclose()

    assert call_count == 1, f"expected single attempt on 404, got {call_count}"


async def test_retries_on_connect_error_then_succeeds() -> None:
    """Transient network failure recovers on next attempt."""
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("nope")
        return httpx.Response(201, json={"id": 1})

    adapter = GitHubAdapter(
        webhook_secret=_SECRET,
        auth=_FakeAuth(),  # type: ignore[arg-type]
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        result = await adapter.reply(_event(), "hi")
        assert result == {"id": 1}
        assert attempts == 2
    finally:
        await adapter.aclose()

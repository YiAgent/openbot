"""FastAPI application entry point.

PRD §5.1 ingress order:
    raw body → verify_signature → parse → (dedup) → (enqueue) → 202

v0.1 Week 1 wiring: /health + /webhook/github. Dedup-by-delivery-id and
Redis enqueue land in Day 2-3; for now the endpoint returns 202 once the
event is normalized to a UnifiedEvent.

Write-back capability (reply / labels / role) is wired only when both
`OPENBOT_GITHUB_APP_ID` and `OPENBOT_GITHUB_APP_PRIVATE_KEY_PATH` are set.
The webhook endpoint itself works without them — useful for receive-only
e2e testing before the App's private key is provisioned.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request, status

from openbot import __version__
from openbot.adapters.base import SignatureError
from openbot.adapters.github import GitHubAdapter
from openbot.adapters.github_auth import GitHubAppAuth
from openbot.config import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_logger = logging.getLogger("openbot.webapp")


def _build_auth(settings: Settings) -> GitHubAppAuth | None:
    """Construct the App auth iff both id and key are configured."""
    if settings.github_app_id is None or settings.github_app_private_key_path is None:
        return None
    return GitHubAppAuth.from_pem_file(
        app_id=settings.github_app_id,
        private_key_path=settings.github_app_private_key_path,
        user_agent=f"OpenBot/{__version__}",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    auth = _build_auth(settings)
    app.state.github_auth = auth
    app.state.github_adapter = (
        GitHubAdapter(
            webhook_secret=settings.github_webhook_secret.get_secret_value(),
            auth=auth,
        )
        if settings.github_webhook_secret is not None
        else None
    )
    _logger.info(
        "openbot_startup",
        extra={
            "version": __version__,
            "webhook_configured": settings.github_webhook_secret is not None,
            "write_back_configured": auth is not None,
        },
    )
    try:
        yield
    finally:
        adapter: GitHubAdapter | None = app.state.github_adapter
        if adapter is not None:
            await adapter.aclose()
        if auth is not None:
            await auth.aclose()


app = FastAPI(
    title="OpenBot",
    description=(
        "Open-source, self-hosted, BYO-API-key GitHub maintenance bot. "
        "See docs/prd/openbot-prd.md for the spec."
    ),
    version=__version__,
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — must stay cheap, no I/O."""
    return {"status": "ok", "version": __version__}


@app.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(request: Request) -> dict[str, str | int | bool]:
    """Receive a GitHub webhook.

    Order matters (PRD §5.1):
      1. read RAW body bytes — HMAC is computed over them
      2. verify signature (constant-time)
      3. parse → UnifiedEvent
      4. enqueue + dedup-by-delivery_id (NOT YET — Day 2-3)
      5. return 202 immediately so GitHub doesn't retry
    """
    adapter: GitHubAdapter | None = getattr(request.app.state, "github_adapter", None)
    if adapter is None:
        # Webhooks intentionally off until setup.sh has run.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OPENBOT_GITHUB_WEBHOOK_SECRET is not set",
        )

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        adapter.verify_signature(body, headers)
    except SignatureError as exc:
        # Audit: log the failed delivery_id so attacker probes are visible.
        _logger.warning(
            "webhook_signature_rejected",
            extra={"delivery_id": headers.get("x-github-delivery", "?"), "reason": str(exc)},
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    event = adapter.parse_event(body, headers)

    # Structured audit log — never include body / token / actor email.
    _logger.info(
        "webhook_accepted",
        extra={
            "channel": event.channel,
            "delivery_id": event.delivery_id,
            "kind": event.kind.value,
            "repo": event.repo,
            "actor": event.actor,
            "installation_id": event.installation_id,
            "relevant": event.is_relevant,
        },
    )

    return {
        "status": "accepted",
        "delivery_id": event.delivery_id,
        "kind": event.kind.value,
        "relevant": event.is_relevant,
    }

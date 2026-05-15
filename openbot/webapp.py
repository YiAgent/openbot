"""FastAPI application entry point.

PRD §5.1 ingress order:
    raw body → verify_signature → parse → dedup → schedule workflow → 202

v0.1 wiring: /health + /webhook/github.
  - Webhook dedup via Redis SET NX EX is live in this slice — duplicate
    retries from GitHub no longer re-run the workflow.
  - Workflow dispatch still uses FastAPI BackgroundTasks; the Redis queue
    + delivery_id-keyed worker land in the middleware slice.

Write-back capability (reply / labels / role) is wired only when both
`OPENBOT_GITHUB_APP_ID` and `OPENBOT_GITHUB_APP_PRIVATE_KEY_PATH` are set.
The webhook endpoint itself works without them — useful for receive-only
e2e testing before the App's private key is provisioned.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status

from openbot import __version__
from openbot.adapters.base import SignatureError
from openbot.adapters.github import GitHubAdapter
from openbot.adapters.github_auth import GitHubAppAuth
from openbot.config import Settings, get_settings
from openbot.persistence import DedupOutcome, WebhookDedup, make_client
from openbot.workflows import maybe_run_triage

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import redis.asyncio as redis_async

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

    redis_client: redis_async.Redis | None = (
        make_client(settings.redis_url) if settings.redis_url else None
    )
    app.state.redis = redis_client
    app.state.dedup = WebhookDedup(redis_client)

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
            "redis_configured": redis_client is not None,
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
        if redis_client is not None:
            await redis_client.aclose()


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
async def github_webhook(
    request: Request, background: BackgroundTasks
) -> dict[str, str | int | bool]:
    """Receive a GitHub webhook.

    Order matters (PRD §5.1):
      1. read RAW body bytes — HMAC is computed over them
      2. verify signature (constant-time)
      3. parse → UnifiedEvent
      4. dedup via Redis SET NX EX — duplicate retries short-circuit with 202
      5. schedule workflow as a background task (runs after we 202)
      6. return 202 immediately so GitHub doesn't retry

    BackgroundTasks remains the v0.1 stop-gap; a Redis queue + delivery_id
    keyed worker lands in the middleware slice so workflows survive a
    process restart.
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

    # Dedup: if GitHub re-delivers the same X-GitHub-Delivery (our slow handler
    # or any 5xx makes them retry), we still 202 but skip workflow dispatch.
    # The dedup falls open when Redis is unconfigured/down — see WebhookDedup docstring.
    dedup: WebhookDedup = request.app.state.dedup
    outcome = await dedup.check_and_mark(event.channel, event.delivery_id)
    if outcome is DedupOutcome.DUPLICATE:
        _logger.info(
            "webhook_duplicate_dropped",
            extra={
                "channel": event.channel,
                "delivery_id": event.delivery_id,
                "kind": event.kind.value,
            },
        )
        return {
            "status": "duplicate",
            "delivery_id": event.delivery_id,
            "kind": event.kind.value,
            "relevant": event.is_relevant,
        }

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
            "dedup_outcome": outcome.value,
        },
    )

    # Dispatch workflows. Each handler decides whether the event qualifies;
    # `maybe_run_*` are designed to be idempotent + swallow their own failures.
    background.add_task(maybe_run_triage, adapter, event)

    return {
        "status": "accepted",
        "delivery_id": event.delivery_id,
        "kind": event.kind.value,
        "relevant": event.is_relevant,
    }

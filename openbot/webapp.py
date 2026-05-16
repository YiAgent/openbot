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
from openbot.config_repo import load_for_repo
from openbot.middleware import PreflightContext, run_preflight
from openbot.persistence import (
    DedupOutcome,
    WebhookDedup,
    create_schema,
    make_client,
    make_engine,
    make_session_factory,
)
from openbot.router import dispatch_for

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import redis.asyncio as redis_async
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from openbot.events import UnifiedEvent
    from openbot.router import Dispatch

_logger = logging.getLogger("openbot.webapp")


def _build_auth(settings: Settings) -> GitHubAppAuth | None:
    """Construct the App auth iff both id and key are configured.

    Returns None (with WARNING) when the PEM path is set but the file is
    inaccessible — missing, wrong permissions, mounted as a directory, stale
    NFS handle, anything else under `OSError`. Webhooks still 503 cleanly
    via the github_webhook_secret check; write-back is just disabled until
    the user fixes their .env.

    The catch is broad (`OSError`) rather than narrow (`FileNotFoundError`)
    because first-run users routinely produce ALL the subclasses — wrong
    chmod, dir typo, etc. — and crashing startup on each variant gives no
    useful signal beyond what the WARNING log already provides.
    """
    if settings.github_app_id is None or settings.github_app_private_key_path is None:
        return None
    try:
        return GitHubAppAuth.from_pem_file(
            app_id=settings.github_app_id,
            private_key_path=settings.github_app_private_key_path,
            user_agent=f"OpenBot/{__version__}",
        )
    except OSError as exc:
        _logger.warning(
            "github_app_pem_unreadable",
            extra={
                "path": str(settings.github_app_private_key_path),
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )
        return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    auth = _build_auth(settings)

    redis_client: redis_async.Redis | None = (
        make_client(settings.redis_url) if settings.redis_url else None
    )
    app.state.redis = redis_client
    app.state.dedup = WebhookDedup(redis_client)

    db_engine: AsyncEngine | None = None
    db_session_factory: async_sessionmaker[AsyncSession] | None = None
    if settings.postgres_url:
        db_engine = make_engine(settings.postgres_url, echo=settings.debug)
        # First-run schema creation. Idempotent — safe to call on every startup.
        # When the first schema CHANGE ships we replace this with alembic.
        await create_schema(db_engine)
        db_session_factory = make_session_factory(db_engine)
    app.state.db_engine = db_engine
    app.state.db_session_factory = db_session_factory

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
            "postgres_configured": db_engine is not None,
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
        if db_engine is not None:
            await db_engine.dispose()


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

    # Router → workflow dispatch (harness spec §3 M2).
    # `dispatch_for` is pure: no GitHub API calls, no DB. If the event
    # has no matching handler (e.g. push, star, bot author) we still
    # 202 — GitHub only needs to know we received the delivery.
    dispatch = dispatch_for(event)
    if dispatch is None:
        return {
            "status": "ignored",
            "delivery_id": event.delivery_id,
            "kind": event.kind.value,
            "relevant": event.is_relevant,
        }

    # Pre-flight + workflow dispatch run in the background so the
    # webhook can 202 within GitHub's 10s budget. The config load
    # (GitHub Contents API) lives inside the background task because
    # it can take ~50ms and the 202 is more important than fresh config.
    background.add_task(
        _run_dispatch,
        request.app,
        adapter,
        event,
        dispatch,
    )

    return {
        "status": "accepted",
        "delivery_id": event.delivery_id,
        "kind": event.kind.value,
        "feature": dispatch.feature.value,
        "task_id": dispatch.task_id,
        "relevant": event.is_relevant,
    }


async def _run_dispatch(
    app_instance: FastAPI,
    adapter: GitHubAdapter,
    event: UnifiedEvent,
    dispatch: Dispatch,
) -> None:
    """Background task: load config → pre-flight → handler.

    Lives in webapp.py rather than middleware/preflight.py so the
    pre-flight runner stays test-friendly (it accepts a ready
    `PreflightContext` and a middleware iterable; the webapp is
    responsible for assembling both).

    Errors here must NEVER raise out — the webhook already returned
    202 and a stack trace at this layer would just go to logs.
    """
    try:
        config = await load_for_repo(adapter, event)
    except Exception:
        _logger.exception(
            "preflight_config_load_failed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
        return

    ctx = PreflightContext(
        event=event,
        dispatch=dispatch,
        config=config,
        adapter=adapter,
        session_factory=getattr(app_instance.state, "db_session_factory", None),
        redis=getattr(app_instance.state, "redis", None),
    )

    # Slice A: pre-flight middleware list is empty — every dispatched
    # event reaches its workflow. Slices B+C plug real gates in here.
    middlewares: list = []
    try:
        decision = await run_preflight(ctx, middlewares)
    except Exception:
        _logger.exception(
            "preflight_runner_crashed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
        return

    from openbot.middleware import MiddlewareResult

    if decision.result is not MiddlewareResult.PROCEED:
        # Audit + comment already handled by run_preflight; nothing else.
        return

    try:
        await dispatch.handler(ctx)
    except Exception:
        _logger.exception(
            "workflow_handler_crashed",
            extra={
                "delivery_id": event.delivery_id,
                "repo": event.repo,
                "feature": dispatch.feature.value,
            },
        )

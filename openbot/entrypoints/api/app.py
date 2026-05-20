"""FastAPI application factory.

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
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from openbot import __version__
from openbot.application.ports.channel_adapter import ChannelAdapterPort
from openbot.application.ports.dedup import DedupPort
from openbot.application.ports.queue import QueuePort
from openbot.core.logging import configure_root_logger
from openbot.core.settings import Settings, get_settings
from openbot.entrypoints.api.routes.github_webhook import router as _webhook_router
from openbot.entrypoints.api.routes.health import router as _health_router
from openbot.infrastructure.adapters.github import GitHubAdapter
from openbot.infrastructure.adapters.github_auth import GitHubAppAuth
from openbot.infrastructure.config_loader import YamlConfigLoader
from openbot.infrastructure.observability import init_langsmith, init_sentry
from openbot.infrastructure.persistence import (
    WebhookDedup,
    create_schema,
    make_client,
    make_engine,
    make_session_factory,
)
from openbot.infrastructure.persistence.cancellation_redis import RedisCancellation
from openbot.infrastructure.persistence.rate_limiter_redis import RedisRateLimiter
from openbot.infrastructure.persistence.resource_lock_redis import RedisResourceLock
from openbot.infrastructure.queue.enqueue import RedisStreamQueue

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import redis.asyncio as redis_async
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_logger = logging.getLogger("openbot.entrypoints.api.app")
_TRUSTED_HOSTS = ["localhost", "127.0.0.1", "test", "testserver", "*.herokuapp.com"]


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
    if settings.github_app_id is None:
        return None
    pem_secret = settings.github_app_private_key_pem
    if pem_secret is None and settings.github_app_private_key_path is None:
        return None
    try:
        return GitHubAppAuth.from_pem_or_path(
            app_id=settings.github_app_id,
            private_key_pem=pem_secret.get_secret_value() if pem_secret else None,
            private_key_path=settings.github_app_private_key_path,
            user_agent=f"OpenBot/{__version__}",
        )
    except (OSError, ValueError) as exc:
        _logger.warning(
            "github_app_pem_unreadable",
            extra={
                "source": "pem" if pem_secret else "path",
                "path": str(settings.github_app_private_key_path)
                if settings.github_app_private_key_path
                else None,
                "reason": f"{type(exc).__name__}: {exc}",
            },
        )
        return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Switch root logger to JSON before any other startup work so every
    # subsequent log line (sentry_initialised, langsmith_tracing_active,
    # openbot_startup, etc.) is emitted as a structured JSON object.
    configure_root_logger()
    settings = get_settings()
    # Initialise Sentry first so any later startup error (Postgres
    # unreachable, malformed PEM, etc.) is captured.
    init_sentry(settings, component="webapp")
    init_langsmith()
    try:
        import sentry_sdk as _sentry

        _sentry.profiler.start_profiler()
    except Exception:
        _logger.debug("sentry_profiler_start_failed", exc_info=True)
    auth = _build_auth(settings)

    redis_client: redis_async.Redis | None = (
        make_client(settings.redis_url) if settings.redis_url else None
    )
    app.state.redis = redis_client
    app.state.cancellation = RedisCancellation(redis_client)
    app.state.resource_lock = RedisResourceLock(redis_client)
    dedup: DedupPort = WebhookDedup(redis_client)
    app.state.dedup = dedup
    queue: QueuePort = RedisStreamQueue(redis_client)
    app.state.queue = queue
    rate_limiter = RedisRateLimiter(redis_client)
    app.state.rate_limiter = rate_limiter
    config_loader = YamlConfigLoader()
    app.state.config_loader = config_loader

    db_engine: AsyncEngine | None = None
    db_session_factory: async_sessionmaker[AsyncSession] | None = None
    if settings.postgres_url:
        db_engine = make_engine(settings.postgres_url, echo=settings.debug)
        # First-run schema creation. Idempotent — safe to call on every startup.
        # Matches the explicit `make db-init` command so manual bootstrap and
        # runtime bootstrap use the same helper.
        await create_schema(db_engine)
        db_session_factory = make_session_factory(db_engine)
    app.state.db_engine = db_engine
    app.state.db_session_factory = db_session_factory

    from openbot.infrastructure.persistence.audit_log_impl import SqlAuditLog
    from openbot.infrastructure.persistence.runs_repo_impl import SqlRunsRepo

    if db_session_factory is not None:
        app.state.runs_repo = SqlRunsRepo(db_session_factory)
        app.state.audit = SqlAuditLog(db_session_factory)
    else:
        app.state.runs_repo = None
        app.state.audit = None

    app.state.github_auth = auth
    github_adapter: ChannelAdapterPort | None = (
        GitHubAdapter(
            webhook_secret=settings.github_webhook_secret.get_secret_value(),
            auth=auth,
        )
        if settings.github_webhook_secret is not None
        else None
    )
    app.state.github_adapter = github_adapter
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
        try:
            import sentry_sdk as _sentry

            _sentry.profiler.stop_profiler()
        except Exception:
            _logger.debug("sentry_profiler_stop_failed", exc_info=True)
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
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_TRUSTED_HOSTS)


@app.middleware("http")
async def attach_request_id(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Attach a stable request id to request.state and the response.

    GitHub webhook requests already carry ``X-GitHub-Delivery``; reusing it
    gives us cross-system correlation without inventing another identifier.
    """
    request_id = (
        request.headers.get("x-github-delivery")
        or request.headers.get("x-request-id")
        or uuid4().hex
    )
    request.state.request_id = request_id

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def log_http_request(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Emit one ingress log per request.

    Uvicorn access logs are disabled in Procfile, so this becomes the
    request-level visibility for Heroku / Papertrail.
    """
    started = perf_counter()
    response = await call_next(request)
    elapsed_ms = round((perf_counter() - started) * 1000, 2)
    _logger.info(
        "http_request_completed",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "request_id": getattr(request.state, "request_id", None),
            "delivery_id": request.headers.get("x-github-delivery"),
            "host": request.headers.get("host"),
            "elapsed_ms": elapsed_ms,
        },
    )
    # Record Sentry metrics for high-level visibility without Prometheus
    from openbot.core.sentry_metrics import metrics

    metrics.incr(
        "http.request.count",
        tags={
            "method": request.method,
            "path": request.url.path,
            "status": str(response.status_code),
        },
    )
    metrics.distribution(
        "http.request.duration",
        elapsed_ms,
        unit="millisecond",
        tags={"method": request.method, "path": request.url.path},
    )
    return response


def _attach_prometheus_metrics(app: FastAPI) -> None:
    """Mount ``/metrics`` with the default Prometheus instrumentator.

    Wrapped in a try/except so a missing ``prometheus-fastapi-instrumentator``
    in a slim image fails open (warning + no /metrics endpoint) rather than
    breaking app boot. /metrics is unauthenticated by design — Heroku's
    metrics endpoint is fronted by their own auth; for self-host setups the
    user should fence /metrics off in their reverse proxy.
    """
    try:
        from prometheus_fastapi_instrumentator import Instrumentator
    except ImportError:
        _logger.warning("prometheus_instrumentator_not_installed")
        return

    # Default config exposes request count + latency histograms keyed by
    # (handler, method, status). No body sampling, no PII. ``expose`` adds
    # the /metrics route; ``instrument`` wires the request middleware.
    instrumentator = Instrumentator(
        excluded_handlers=["/metrics"],  # don't measure the meta endpoint
    )
    instrumentator.instrument(app).expose(app, include_in_schema=False, tags=["meta"])


_attach_prometheus_metrics(app)

app.include_router(_health_router)
app.include_router(_webhook_router)

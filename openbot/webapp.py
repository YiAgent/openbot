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
from openbot.dispatch import run_dispatch
from openbot.obs import init_sentry
from openbot.persistence import (
    DedupOutcome,
    WebhookDedup,
    create_schema,
    make_client,
    make_engine,
    make_session_factory,
)
from openbot.queue import QueuePayload, enqueue
from openbot.router import Dispatch, derive_run_id, dispatch_for, upgrade_dispatch
from openbot.state import Intent
from openbot.state.cancellation import signal as cancellation_signal
from openbot.state.resource_lock import resource_lock
from openbot.state.runs_repo import TransitionResult, transition

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
    settings = get_settings()
    # Initialise Sentry first so any later startup error (Postgres
    # unreachable, malformed PEM, etc.) is captured.
    init_sentry(settings, component="webapp")
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


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — must stay cheap, no I/O."""
    return {"status": "ok", "version": __version__}


@app.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request, background: BackgroundTasks
) -> dict[str, str | int | bool | None]:
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

    try:
        event = adapter.parse_event(body, headers)
    except SignatureError as exc:
        # parse_event raises SignatureError for "valid HMAC + invalid JSON"
        # by design (github.py docstring): a passer-by who can sign but
        # sends garbage is a more pointed probe than one who can't sign at
        # all, so we collapse it into the same 401 lane rather than 500.
        # Distinct log key so dashboards can separate the two failure
        # modes — alert on bad-JSON-after-good-HMAC differently than
        # plain HMAC-mismatch noise.
        _logger.warning(
            "webhook_payload_unparseable",
            extra={"delivery_id": headers.get("x-github-delivery", "?"), "reason": str(exc)},
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

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

    # State-machine classification: only runs when both Postgres (for
    # ``task_runs``) and a stateful resource_key (issue/PR number, not
    # ping/push) are present. The two-tier guard means dev mode
    # without docker-compose and integration tests without a DB still
    # exercise the original v1 path without crashing.
    db_factory = getattr(request.app.state, "db_session_factory", None)
    redis_client = getattr(request.app.state, "redis", None)
    transition_result: TransitionResult | None = None
    if db_factory is not None and event.resource_key is not None:
        try:
            dispatch, transition_result = await _classify_and_upgrade(
                event=event,
                dispatch=dispatch,
                session_factory=db_factory,
                redis=redis_client,
            )
        except Exception:
            # State-machine path failed (DB unreachable, etc.). Audit
            # and fall back to the v1 path so we never drop a webhook
            # because of a Postgres flap.
            _logger.exception(
                "state_machine_transition_failed",
                extra={"delivery_id": event.delivery_id, "repo": event.repo},
            )
            transition_result = None
        else:
            # IGNORE → no work to enqueue; the row (if any) was already
            # written under the lock. Still return 202 to GitHub.
            if transition_result.classification.intent is Intent.IGNORE:
                _logger.info(
                    "state_machine_ignored",
                    extra={
                        "delivery_id": event.delivery_id,
                        "resource_key": event.resource_key,
                        "reason": transition_result.classification.reason,
                    },
                )
                return {
                    "status": "ignored",
                    "delivery_id": event.delivery_id,
                    "kind": event.kind.value,
                    "intent": transition_result.classification.intent.value,
                    "reason": transition_result.classification.reason,
                    "resource_key": event.resource_key,
                }
            # SUPERSEDE / CANCEL: signal the prior run so workers on
            # any dyno can stop early at their next checkpoint. We
            # signal from the receive side too (in addition to the
            # worker doing it on dequeue) so even a long queue lag
            # can't delay the cancel.
            if transition_result.prev_run_id and redis_client is not None:
                try:
                    await cancellation_signal(redis_client, transition_result.prev_run_id)
                except Exception:
                    _logger.exception(
                        "cancellation_signal_failed",
                        extra={
                            "delivery_id": event.delivery_id,
                            "prev_run_id": transition_result.prev_run_id,
                        },
                    )

    # Immediate feedback (GitHub Check Run) for PR events.
    # We create the check run synchronously in the webhook handler so the
    # check_run_id is available for the worker's updates.
    check_run_id: int | None = None
    if event.pr_number and request.app.state.github_auth is not None:
        # Extract head_sha from the PR payload.
        head_sha = (event.raw.get("pull_request") or {}).get("head", {}).get("sha")
        if head_sha:
            try:
                check_run = await adapter.create_check_run(
                    event,
                    name="OpenBot Analysis",
                    head_sha=head_sha,
                    output={
                        "title": "Starting Analysis...",
                        "summary": (
                            f"OpenBot has received delivery `{event.delivery_id}` and is "
                            f"dispatching the `{dispatch.feature.value}` workflow."
                        ),
                    },
                )
                check_run_id = check_run.get("id")
            except Exception:
                # Failing to create a check run shouldn't stop the workflow dispatch.
                _logger.exception(
                    "check_run_creation_failed",
                    extra={"delivery_id": event.delivery_id, "repo": event.repo},
                )

    # Hand off to the worker queue if Redis is configured; otherwise
    # fall back to FastAPI BackgroundTasks (dev / unit tests).
    #
    # The queue path is what production runs — workflows survive a
    # webapp restart because the entry persists in Redis until a
    # worker XACKs it. The fallback exists so `make dev` without
    # docker-compose still gives a working bot.
    # ``redis_client`` was already read above for the state-machine
    # path; reuse it here so we don't double-tap ``app.state``.
    if redis_client is not None:
        try:
            payload = QueuePayload.from_event(
                event,
                feature=dispatch.feature,
                task_id=dispatch.task_id,
                check_run_id=check_run_id,
                intent=dispatch.intent,
                run_id=dispatch.run_id,
                prev_run_id=dispatch.prev_run_id,
                resource_key=dispatch.resource_key,
                event_seq=dispatch.event_seq,
            )
            entry_id = await enqueue(redis_client, payload)
            return {
                "status": "accepted",
                "delivery_id": event.delivery_id,
                "kind": event.kind.value,
                "feature": dispatch.feature.value,
                "task_id": dispatch.task_id,
                "entry_id": entry_id,
                "relevant": event.is_relevant,
                "check_run_id": check_run_id,
            }
        except Exception:
            # Redis enqueue failed (RDB save in progress, OOM, etc.).
            # Don't 5xx the webhook — degrade to BackgroundTask so the
            # event isn't lost. The next event will retry the queue path.
            _logger.exception(
                "queue_enqueue_failed_falling_back_to_background_task",
                extra={"delivery_id": event.delivery_id, "repo": event.repo},
            )

    background.add_task(
        _run_dispatch,
        request.app,
        adapter,
        event,
        dispatch,
        check_run_id,
    )

    return {
        "status": "accepted",
        "delivery_id": event.delivery_id,
        "kind": event.kind.value,
        "feature": dispatch.feature.value,
        "task_id": dispatch.task_id,
        "relevant": event.is_relevant,
        "check_run_id": check_run_id,
    }


async def _classify_and_upgrade(
    *,
    event: UnifiedEvent,
    dispatch: Dispatch,
    session_factory: async_sessionmaker[AsyncSession],
    redis: redis_async.Redis | None,
) -> tuple[Dispatch, TransitionResult]:
    """Run the state-machine classifier under the per-resource lock.

    Three concerns are bundled here so the webhook handler stays
    flat:

      1. ``resource_lock`` keeps two concurrent deliveries for the
         same resource from racing past each other into a double-START.
      2. ``runs_repo.transition`` does the CAS-guarded write under
         the same transaction.
      3. ``upgrade_dispatch`` carries the classifier's intent +
         run identity into the Dispatch so the queue payload and the
         eventual handler invocation see them.

    The function commits the session on success — a single transaction
    boundary covers both the read-prior-row and the write-new-row so
    that the SQLAlchemy ``version_id_col`` CAS check sees the same
    snapshot. Returning the ``TransitionResult`` lets the caller
    branch on ``Intent.IGNORE`` without re-reading the row.
    """
    assert event.resource_key is not None  # gated by the caller
    # ``time.monotonic_ns()`` gives a strictly monotonic per-process
    # serial so two simultaneous classified events for the same
    # resource end up with distinct ``run_id`` hashes even when the
    # clock granularity is coarse.
    import time

    serial = time.monotonic_ns()
    new_run_id = derive_run_id(event.resource_key, serial)

    async with (
        resource_lock(redis, event.resource_key) as _acquired,
        session_factory() as session,
    ):
        result = await transition(session, event=event, new_run_id=new_run_id)
        # transition() does NOT commit — keeping the boundary here
        # means the CAS check and the row write see the same
        # transaction snapshot.
        await session.commit()

    upgraded = upgrade_dispatch(
        dispatch,
        intent=result.classification.intent.value,
        run_id=result.run_id or new_run_id,
        prev_run_id=result.prev_run_id,
        event_seq=event.event_seq,
        resource_key=event.resource_key,
    )
    return upgraded, result


async def _run_dispatch(
    app_instance: FastAPI,
    adapter: GitHubAdapter,
    event: UnifiedEvent,
    dispatch: Dispatch,
    check_run_id: int | None = None,
) -> None:
    """In-process dispatch — used as the fallback when Redis is absent.

    Production runs through the Redis queue worker (``openbot.queue.runner``)
    instead; this path exists so `make dev` without docker-compose still
    delivers a working bot and so unit tests don't need fakeredis just
    to exercise the webhook flow.

    The actual middleware chain + handler invocation lives in
    ``openbot.dispatch.run_dispatch`` so the worker and the webapp can't
    drift apart.
    """
    await run_dispatch(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        session_factory=getattr(app_instance.state, "db_session_factory", None),
        redis=getattr(app_instance.state, "redis", None),
        check_run_id=check_run_id,
    )

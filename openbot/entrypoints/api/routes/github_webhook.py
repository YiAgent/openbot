"""POST /webhook/github — signed webhook ingestion.

This module is HTTP glue only:
  1. Read raw body bytes — HMAC is computed over them.
  2. Verify signature (constant-time).
  3. Parse → UnifiedEvent.
  4. Call the ``ingest_webhook`` use-case (orchestration lives there).
  5. If the use-case flagged a BackgroundTask fallback, add it here.
  6. Return 202.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status

from openbot.application.dispatcher import run_dispatch
from openbot.application.use_cases.ingest_webhook import ingest_webhook
from openbot.entrypoints.api.deps import verified_github_event
from openbot.infrastructure.adapters.github import GitHubAdapter

if TYPE_CHECKING:
    from openbot.application.router import Dispatch
    from openbot.domain.events import UnifiedEvent

router = APIRouter()
_verified_github_event = Depends(verified_github_event)


@router.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    background: BackgroundTasks,
    event: UnifiedEvent = _verified_github_event,
) -> dict[str, str | int | bool | None]:
    """Receive a GitHub webhook.

    Order matters (PRD §5.1):
      1. Read RAW body bytes — HMAC is computed over them.
      2. Verify signature (constant-time).
      3. Parse → UnifiedEvent.
      4. Call ingest_webhook use-case (dedup, classify, enqueue, etc.).
      5. If the use-case signals a BackgroundTask fallback, schedule it here.
      6. Return 202 immediately so GitHub doesn't retry.

    BackgroundTasks remains the v0.1 stop-gap; a Redis queue + delivery_id
    keyed worker lands in the middleware slice so workflows survive a
    process restart.
    """
    adapter: GitHubAdapter = request.app.state.github_adapter

    state = request.app.state
    result = await ingest_webhook(
        event,
        adapter,
        dedup=state.dedup,
        runs_repo=getattr(state, "runs_repo", None),
        resource_lock=getattr(state, "resource_lock", None),
        cancellation=getattr(state, "cancellation", None),
        queue=getattr(state, "queue", None),
        github_auth_configured=getattr(state, "github_auth", None) is not None,
        redis_client=getattr(state, "redis", None),
    )

    if result.background_dispatch is not None:
        bd = result.background_dispatch
        background.add_task(
            _run_dispatch,
            request.app,
            adapter,
            bd.event,
            bd.dispatch,
            bd.check_run_id,
            getattr(state, "audit", None),
        )

    return result.to_response()


async def _run_dispatch(
    app_instance: object,
    adapter: GitHubAdapter,
    event: UnifiedEvent,
    dispatch: Dispatch,
    check_run_id: int | None = None,
    audit: object | None = None,
) -> None:
    """In-process dispatch — used as the fallback when Redis is absent.

    Production runs through the Redis queue worker (``openbot.infrastructure.queue.runner``)
    instead; this path exists so `make dev` without docker-compose still
    delivers a working bot and so unit tests don't need fakeredis just
    to exercise the webhook flow.

    The actual middleware chain + handler invocation lives in
    ``openbot.application.dispatcher.run_dispatch`` so the worker and the webapp can't
    drift apart.
    """
    await run_dispatch(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        session_factory=getattr(app_instance.state, "db_session_factory", None),
        redis=getattr(app_instance.state, "redis", None),
        check_run_id=check_run_id,
        audit=audit,
        rate_limiter=getattr(app_instance.state, "rate_limiter", None),
        config_loader=getattr(app_instance.state, "config_loader", None),
    )

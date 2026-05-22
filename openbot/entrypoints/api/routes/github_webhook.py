"""POST /webhook/github — signed webhook ingestion.

This module is HTTP glue only:
  1. Read raw body bytes — HMAC is computed over them.
  2. Verify signature (constant-time).
  3. Parse → UnifiedEvent.
  4. Call the ``ingest_webhook`` use-case (orchestration lives there).
  5. Return 202.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request, status

from openbot.application.use_cases.ingest_webhook import ingest_webhook
from openbot.entrypoints.api.deps import verified_github_event
from openbot.infrastructure.adapters.github import GitHubAdapter

if TYPE_CHECKING:
    from openbot.domain.events import UnifiedEvent

router = APIRouter()
_verified_github_event = Depends(verified_github_event)


@router.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    event: UnifiedEvent = _verified_github_event,
) -> dict[str, str | int | bool | None]:
    """Receive a GitHub webhook.

    Order matters (PRD §5.1):
      1. Read RAW body bytes — HMAC is computed over them.
      2. Verify signature (constant-time).
      3. Parse → UnifiedEvent.
      4. Call ingest_webhook use-case (dedup, classify, enqueue, etc.).
      5. Return 202 immediately so GitHub doesn't retry.
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

    return result.to_response()

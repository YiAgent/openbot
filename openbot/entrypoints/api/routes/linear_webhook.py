"""POST /webhook/linear — signed webhook ingestion for Linear.

Same pattern as GitHub webhook: verify signature → parse → ingest → 202.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request, status

from openbot.application.use_cases.ingest_webhook import ingest_webhook
from openbot.entrypoints.api.deps import verified_linear_event
from openbot.infrastructure.adapters.linear import LinearAdapter

if TYPE_CHECKING:
    from openbot.domain.events import UnifiedEvent

router = APIRouter()
_verified_linear_event = Depends(verified_linear_event)


@router.post("/webhook/linear", status_code=status.HTTP_202_ACCEPTED)
async def linear_webhook(
    request: Request,
    event: UnifiedEvent = _verified_linear_event,
) -> dict[str, str | int | bool | None]:
    """Receive a Linear webhook.

    Order: verify signature → parse → ingest_webhook → 202.
    """
    adapter: LinearAdapter = request.app.state.linear_adapter

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

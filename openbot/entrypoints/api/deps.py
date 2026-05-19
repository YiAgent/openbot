"""FastAPI dependencies for the API entrypoint.

At the moment this module only exposes the GitHub webhook ingress boundary:
read raw bytes, verify the signature, parse the event, and surface a single
``UnifiedEvent`` to the route handler. Keeping this as a route-local
dependency avoids pushing GitHub-specific trust checks into global middleware.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, status

from openbot.infrastructure.adapters.base import SignatureError
from openbot.infrastructure.adapters.github import GitHubAdapter

if TYPE_CHECKING:
    from openbot.domain.events import UnifiedEvent

_logger = logging.getLogger(__name__)


async def verified_github_event(request: Request) -> UnifiedEvent:
    """Return a parsed GitHub event after verifying the raw webhook body.

    This is intentionally route-local, not app-global: only the GitHub webhook
    endpoint needs the raw-body HMAC trust boundary.
    """
    adapter: GitHubAdapter | None = getattr(request.app.state, "github_adapter", None)
    if adapter is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OPENBOT_GITHUB_WEBHOOK_SECRET is not set",
        )

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    request.state.webhook_body = body

    try:
        adapter.verify_signature(body, headers)
    except SignatureError as exc:
        _logger.warning(
            "webhook_signature_rejected",
            extra={
                "delivery_id": headers.get("x-github-delivery", "?"),
                "request_id": getattr(request.state, "request_id", None),
                "reason": str(exc),
            },
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    try:
        event = adapter.parse_event(body, headers)
    except SignatureError as exc:
        _logger.warning(
            "webhook_payload_unparseable",
            extra={
                "delivery_id": headers.get("x-github-delivery", "?"),
                "request_id": getattr(request.state, "request_id", None),
                "reason": str(exc),
            },
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    request.state.github_event = event
    return event

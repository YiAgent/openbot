"""FastAPI dependencies for the API entrypoint.

Exposes webhook ingress boundaries for each channel:
- ``verified_github_event`` — GitHub webhook HMAC verification
- ``verified_linear_event`` — Linear webhook HMAC verification

Each is route-local to avoid pushing channel-specific trust checks into
global middleware.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, status

from openbot.infrastructure.adapters.base import SignatureError
from openbot.infrastructure.adapters.github import GitHubAdapter
from openbot.infrastructure.adapters.linear import LinearAdapter

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


async def verified_linear_event(request: Request) -> UnifiedEvent:
    """Return a parsed Linear event after verifying the webhook signature."""
    adapter: LinearAdapter | None = getattr(request.app.state, "linear_adapter", None)
    if adapter is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OPENBOT_LINEAR_WEBHOOK_SECRET is not set",
        )

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    request.state.webhook_body = body

    try:
        adapter.verify_signature(body, headers)
    except SignatureError as exc:
        _logger.warning(
            "linear_webhook_signature_rejected",
            extra={"reason": str(exc)},
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    try:
        event = adapter.parse_event(body, headers)
    except Exception as exc:
        _logger.warning(
            "linear_webhook_payload_unparseable",
            extra={"reason": str(exc)},
        )
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    request.state.linear_event = event
    return event

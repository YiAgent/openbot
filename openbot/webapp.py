"""FastAPI application entry point.

PRD §5.1 ingress order:
    raw body → verify_signature → parse → (dedup) → (enqueue) → 202

v0.1 Week 1 wiring: /health + /webhook/github. Dedup-by-delivery-id and
Redis enqueue land in Day 2-3; for now the endpoint returns 202 once the
event is normalized to a UnifiedEvent.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, status

from openbot import __version__
from openbot.adapters.base import SignatureError
from openbot.adapters.github import GitHubAdapter
from openbot.config import get_settings

app = FastAPI(
    title="OpenBot",
    description=(
        "Open-source, self-hosted, BYO-API-key GitHub maintenance bot. "
        "See docs/prd/openbot-prd.md for the spec."
    ),
    version=__version__,
)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — must stay cheap, no I/O."""
    return {"status": "ok", "version": __version__}


@app.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(request: Request) -> dict[str, str]:
    """Receive a GitHub webhook.

    Order matters (PRD §5.1):
      1. read RAW body bytes — HMAC is computed over them
      2. verify signature (constant-time)
      3. parse → UnifiedEvent
      4. enqueue + dedup-by-delivery_id (NOT YET — Day 2-3)
      5. return 202 immediately so GitHub doesn't retry
    """
    settings = get_settings()
    secret = settings.github_webhook_secret
    if secret is None:
        # Webhooks intentionally off until setup.sh has run.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OPENBOT_GITHUB_WEBHOOK_SECRET is not set",
        )

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    adapter = GitHubAdapter(webhook_secret=secret.get_secret_value())

    try:
        adapter.verify_signature(body, headers)
    except SignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    event = adapter.parse_event(body, headers)

    return {
        "status": "accepted",
        "delivery_id": event.delivery_id,
        "kind": event.kind.value,
        "relevant": "true" if event.is_relevant else "false",
    }

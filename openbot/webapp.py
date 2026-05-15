"""FastAPI application entry point.

PRD §5.1 ingress responsibilities (to land in subsequent commits):

  webhook → verify_signature → dedup_by_delivery_id → 202 ACK → enqueue Redis

v0.1 Week 1 skeleton: only `/health` so CI can run pytest. Webhook routes
arrive with the GitHubAdapter in Day 3-4 of the v0.1 plan.
"""

from fastapi import FastAPI

from openbot import __version__

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

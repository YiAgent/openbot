"""GET /health — liveness probe."""

from __future__ import annotations

from fastapi import APIRouter

from openbot import __version__

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — must stay cheap, no I/O."""
    return {"status": "ok", "version": __version__}

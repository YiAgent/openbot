"""GET /health and /ready probes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from openbot import __version__
from openbot.core.settings import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — must stay cheap, no I/O."""
    return {"status": "ok", "version": __version__}


async def _check_redis_ready(redis) -> bool:
    if redis is None:
        return False
    try:
        return bool(await redis.ping())
    except Exception:
        return False


async def _check_db_ready(engine) -> bool:
    if engine is None:
        return False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness probe — verifies config + critical dependencies."""
    settings = get_settings()
    components: dict[str, str] = {}

    webhook_ready = settings.github_webhook_secret is not None
    components["webhook"] = "ok" if webhook_ready else "missing_config"

    if settings.redis_url is None:
        redis_ready = False
        components["redis"] = "missing_config"
    else:
        redis_ready = await _check_redis_ready(getattr(request.app.state, "redis", None))
        components["redis"] = "ok" if redis_ready else "unavailable"

    if settings.postgres_url is None:
        db_ready = False
        components["postgres"] = "missing_config"
    else:
        db_ready = await _check_db_ready(getattr(request.app.state, "db_engine", None))
        components["postgres"] = "ok" if db_ready else "unavailable"

    overall_ready = webhook_ready and redis_ready and db_ready
    return JSONResponse(
        status_code=200 if overall_ready else 503,
        content={
            "ready": overall_ready,
            "version": __version__,
            "components": components,
        },
    )

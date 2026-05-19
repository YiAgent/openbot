"""Worker entrypoint — ``python -m openbot.entrypoints.worker``.

Wires the same backends the FastAPI app does (Redis, Postgres, GitHub
App auth + adapter), creates the consumer group, then runs N
``consume_loop`` tasks concurrently via ``asyncio.gather``.

SIGTERM / SIGINT (sent by ``docker compose stop`` or Ctrl-C) sets a
shared ``asyncio.Event``; each consumer loop checks the event between
``XREADGROUP`` calls and exits cleanly. In-flight handlers finish their
current dispatch before the process exits.

Shape mirrors ``openbot.entrypoints.api.app.lifespan`` deliberately so the two
entrypoints share their startup story. A future ``runner`` that swaps
the dispatch layer (e.g. for v0.2 LangGraph + Modal) just changes what
``consume_loop`` does internally — the bootstrap is the same.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import TYPE_CHECKING

from openbot import __version__
from openbot.core.logging import configure_root_logger
from openbot.core.settings import Settings, get_settings
from openbot.infrastructure.adapters.github import GitHubAdapter
from openbot.infrastructure.adapters.github_auth import GitHubAppAuth
from openbot.infrastructure.observability import init_langsmith, init_sentry
from openbot.infrastructure.persistence import (
    create_schema,
    make_client,
    make_engine,
    make_session_factory,
)
from openbot.infrastructure.queue.worker import consume_loop, ensure_consumer_group

if TYPE_CHECKING:
    pass

_logger = logging.getLogger("openbot.entrypoints.worker")


def _build_auth(settings: Settings) -> GitHubAppAuth | None:
    """Same shape as webapp._build_auth; replicated to avoid pulling
    fastapi as a runtime import on the worker."""
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


async def _main() -> int:
    """Boot Redis + Postgres + adapter; run N consumers until SIGTERM."""
    settings = get_settings()
    # Sentry first — captures any subsequent startup crash (bad DSN
    # for Redis/Postgres, malformed PEM, etc.) on the worker dyno.
    init_sentry(settings, component="worker")
    init_langsmith()
    if settings.redis_url is None:
        _logger.error("worker_no_redis_url")
        return 2
    if settings.github_webhook_secret is None:
        _logger.error("worker_no_webhook_secret")
        return 2

    redis_client = make_client(settings.redis_url)
    auth = _build_auth(settings)
    adapter = GitHubAdapter(
        webhook_secret=settings.github_webhook_secret.get_secret_value(),
        auth=auth,
    )

    db_engine = None
    session_factory = None
    if settings.postgres_url:
        db_engine = make_engine(settings.postgres_url, echo=settings.debug)
        # Idempotent — same as the webapp's lifespan and `make db-init`.
        # Whichever process wins the race creates the tables; the others no-op.
        await create_schema(db_engine)
        session_factory = make_session_factory(db_engine)

    await ensure_consumer_group(redis_client)

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # SIGTERM handler must be re-entrant safe; a second signal
        # is treated as a no-op so a SIGTERM-then-SIGINT pair doesn't
        # spam the log. The Event is the single source of truth.
        loop.add_signal_handler(sig, shutdown.set)

    consumers = [
        asyncio.create_task(
            consume_loop(
                redis=redis_client,
                adapter=adapter,
                session_factory=session_factory,
                consumer_name=f"consumer-{i}",
                shutdown=shutdown,
            ),
            name=f"openbot-consumer-{i}",
        )
        for i in range(settings.worker_concurrency)
    ]
    _logger.info(
        "worker_started",
        extra={
            "version": __version__,
            "concurrency": settings.worker_concurrency,
            "postgres_configured": db_engine is not None,
        },
    )

    try:
        await shutdown.wait()
    finally:
        _logger.info("worker_shutting_down")
        for task in consumers:
            task.cancel()
        # Wait for consumers to finish their in-flight iteration; suppress
        # the CancelledError we just sent. A consumer that hangs past
        # 30s gets dropped — better an unclean exit than a stuck pod.
        try:
            await asyncio.wait_for(
                asyncio.gather(*consumers, return_exceptions=True),
                timeout=30,
            )
        except TimeoutError:
            _logger.warning("worker_shutdown_timeout_consumers_dropped")

        await adapter.aclose()
        if auth is not None:
            await auth.aclose()
        if db_engine is not None:
            await db_engine.dispose()
        await redis_client.aclose()
        _logger.info("worker_stopped")
    return 0


def main() -> int:
    configure_root_logger()
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())

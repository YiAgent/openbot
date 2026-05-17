"""Observability bootstrap — Sentry init shared by webapp + worker.

Two entry processes (``openbot.webapp:app`` and ``openbot.queue.runner``)
both need Sentry attached. Centralising the init here keeps the contract
in one place:

  - DSN unset → ``sentry_sdk.init(dsn=None)`` is a documented no-op
    (https://docs.sentry.io/platforms/python/configuration/options/#dsn).
    Local ``make dev`` and CI runs therefore never need a Sentry project.
  - Domain audit rows (workflow STARTED / COMPLETED / SKIPPED) stay in
    Postgres ``audit_log``. Sentry only sees uncaught exceptions and
    httpx 5xx — that boundary matches PRD §9.4 (audit_log is the
    product-facing ledger; Sentry is the on-call tool).
  - ``send_default_pii=False`` is the default in sentry-sdk 2.x; we keep
    it explicit so a future bump can't quietly start exfiltrating
    request bodies (which may carry webhook payloads).

Integrations: ``FastApiIntegration`` (auto-instruments ASGI exceptions),
``HttpxIntegration`` (tracks GitHub API call latency / status). Neither
needs a sample rate to capture errors — ``traces_sample_rate`` only
gates *performance* spans.

The ``init_sentry`` call is idempotent: calling it twice with the same
DSN re-uses the existing hub. The worker process imports this module
and calls init from ``runner._main`` before any other work.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openbot.config import Settings

_logger = logging.getLogger(__name__)


def init_sentry(settings: Settings, *, component: str) -> None:
    """Initialise Sentry from Settings. Safe to call with no DSN.

    ``component`` ("webapp" / "worker") is attached as a tag so the
    Sentry UI can split alerts by process — a 5xx burst from the
    worker is a different on-call signal than one from the webapp.
    """
    dsn = settings.sentry_dsn.get_secret_value() if settings.sentry_dsn is not None else None

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.httpx import HttpxIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        # sentry-sdk is in the runtime deps, but a slim install (e.g. a
        # `--no-default-deps` smoke image) might skip it. Log + continue
        # rather than crashing the process — Sentry is opt-in by design.
        _logger.warning("sentry_sdk_not_installed_skipping_init")
        return

    sentry_sdk.init(
        dsn=dsn,  # None = no-op; sentry-sdk documents this contract
        environment=settings.environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        # Webhook bodies may carry repo / actor info that's already in
        # the audit log; do not duplicate into Sentry.
        send_default_pii=False,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
            HttpxIntegration(),
        ],
    )
    sentry_sdk.set_tag("component", component)

    if dsn is not None:
        _logger.info(
            "sentry_initialised",
            extra={
                "component": component,
                "environment": settings.environment,
                "traces_sample_rate": settings.sentry_traces_sample_rate,
            },
        )


__all__ = ["init_sentry"]

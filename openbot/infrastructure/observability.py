"""Observability bootstrap — Sentry + LangSmith init shared by webapp + worker.

Two entry processes (``openbot.entrypoints.api.app:app`` and ``openbot.infrastructure.queue.runner``)
both need Sentry and LangSmith attached. Centralising the init here keeps
the contract in one place:

Sentry
------
  - DSN unset → ``sentry_sdk.init(dsn=None)`` is a documented no-op
    (https://docs.sentry.io/platforms/python/configuration/options/#dsn).
    Local ``make dev`` and CI runs therefore never need a Sentry project.
  - Domain audit rows (workflow STARTED / COMPLETED / SKIPPED) stay in
    Postgres ``audit_log``. Sentry only sees uncaught exceptions and
    httpx 5xx — that boundary matches PRD §9.4 (audit_log is the
    product-facing ledger; Sentry is the on-call tool).
  - Metrics: ``sentry_sdk.metrics`` is enabled by default in 2.x. We
    record business signals (workflow outcomes, LLM costs) there so
    ops has a unified dashboard without needing a Prometheus server.
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

LangSmith
---------
  - Reads ``LANGSMITH_API_KEY`` + ``LANGSMITH_PROJECT`` from the environment
    directly (LangSmith's own env-var convention; no OPENBOT_ prefix).
  - ``LANGSMITH_TRACING=true`` (or ``LANGCHAIN_TRACING_V2=true``) must be
    set to enable trace submission; without it the ``@traceable`` decorators
    are transparent pass-throughs with zero overhead.
  - ``init_langsmith`` just checks + logs whether tracing is active so the
    startup log line makes the status visible.  The actual tracing machinery
    is wired by ``@traceable`` decorators on the use-case functions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openbot.core.settings import Settings

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
        from sentry_sdk.integrations.logging import LoggingIntegration
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
        profiles_sample_rate=settings.sentry_profiles_sample_rate,
        # Webhook bodies may carry repo / actor info that's already in
        # the audit log; do not duplicate into Sentry.
        send_default_pii=False,
        # Forward WARNING+ logs to Sentry Logs (2.35+).
        # Explicit LoggingIntegration overrides the default level=INFO so
        # routine INFO lines (http_request_completed, etc.) stay in
        # stdout only and don't flood the Sentry Logs feed.
        enable_logs=True,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
            HttpxIntegration(),
            LoggingIntegration(
                level=logging.WARNING,  # WARNING+ → breadcrumbs + Sentry Logs
                event_level=logging.ERROR,  # ERROR+   → Sentry error events
            ),
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


def init_langsmith() -> None:
    """Log whether LangSmith tracing is active at startup.

    LangSmith configures itself from env vars; this function exists solely
    to emit a structured startup log so operators know whether tracing is
    on or off — useful when tracing was expected but the env var was missed.

    Active when ALL of the following are present:
      - ``LANGSMITH_API_KEY`` (or ``LANGCHAIN_API_KEY``) — authentication
      - ``LANGSMITH_TRACING=true`` or ``LANGCHAIN_TRACING_V2=true`` — opt-in flag

    The function does NOT fail if langsmith is missing — same graceful
    degradation as Sentry.
    """
    import os

    try:
        import langsmith  # noqa: F401 — existence check only
    except ImportError:
        _logger.warning("langsmith_not_installed_tracing_disabled")
        return

    api_key = os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")
    tracing_enabled = os.environ.get("LANGSMITH_TRACING", "").lower() in {"true", "1"} or (
        os.environ.get("LANGCHAIN_TRACING_V2", "").lower() in {"true", "1"}
    )

    if api_key and tracing_enabled:
        project = os.environ.get("LANGSMITH_PROJECT") or os.environ.get(
            "LANGCHAIN_PROJECT", "default"
        )
        _logger.info(
            "langsmith_tracing_active",
            extra={"project": project},
        )
    elif api_key and not tracing_enabled:
        _logger.info(
            "langsmith_key_present_tracing_off",
            extra={
                "hint": (
                    "Set LANGSMITH_TRACING=true to enable trace submission. "
                    "Current state: key present, tracing flag absent."
                )
            },
        )
    else:
        _logger.info("langsmith_tracing_disabled_no_key")


__all__ = ["init_langsmith", "init_sentry"]

"""Observability bootstrap — Sentry + LangSmith + Langfuse init shared by webapp + worker.

Two entry processes (``openbot.entrypoints.api.app:app`` and ``openbot.infrastructure.queue.runner``)
both need Sentry, LangSmith, and Langfuse attached. Centralising the init here keeps
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

Langfuse
--------
  - Reads ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` from the environment
    (Langfuse's own env-var convention; no OPENBOT_ prefix).
  - Both public and secret key must be set to activate tracing.
  - Host is read in SDK priority order: ``LANGFUSE_BASE_URL`` → ``LANGFUSE_HOST``
    → ``https://cloud.langfuse.com``. Use ``LANGFUSE_BASE_URL`` for the US cloud
    (``https://us.cloud.langfuse.com``) or a self-hosted instance.
  - Two trace layers:
      1. ``@observe`` decorators on use-case entry points (outer span, duration,
         error capture for the whole workflow).
      2. Per-request ``CallbackHandler`` injected into each DeepAgents/LangGraph
         ``ainvoke`` call (inner spans for every agent step + tool call).
  - ``get_langfuse_handler()`` returns a **fresh** ``CallbackHandler()`` per
    invocation — re-using one handler across concurrent requests mixes traces.
    Returns ``None`` when Langfuse is not configured; callers guard with
    ``[h for h in [get_langfuse_handler()] if h is not None]``.
  - Trace name and session grouping are set via LangChain ``RunnableConfig``
    metadata keys ``"langfuse_trace_name"`` / ``"langfuse_session_id"`` so
    Langfuse's ``CallbackHandler`` picks them up automatically from the run
    context.  This keeps each invocation's spans in its own fresh trace while
    still grouping retries / eval re-runs by session.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

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
        profile_session_sample_rate=settings.sentry_profile_session_sample_rate,
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


def init_langfuse() -> None:
    """Log whether Langfuse tracing is active at startup.

    Active when ALL of the following env vars are present:
      - ``LANGFUSE_PUBLIC_KEY`` — project public key (starts with ``pk-lf-``)
      - ``LANGFUSE_SECRET_KEY`` — project secret key (starts with ``sk-lf-``)

    Host resolution mirrors the SDK's own priority (first match wins):
      1. ``LANGFUSE_BASE_URL`` — preferred alias used by Langfuse v4+
      2. ``LANGFUSE_HOST`` — legacy alias still accepted by the SDK
      3. ``https://cloud.langfuse.com`` — SDK default (EU cloud)

    Override to ``https://us.cloud.langfuse.com`` for the US-region cloud,
    or supply your self-hosted URL.

    The function does NOT fail if langfuse is missing — same graceful
    degradation as Sentry and LangSmith.
    """
    import os

    try:
        import langfuse  # noqa: F401 — existence check only
    except ImportError:
        _logger.warning("langfuse_not_installed_tracing_disabled")
        return

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    # Mirror the SDK's own env-var priority so the logged host matches
    # what the SDK actually connects to (LANGFUSE_BASE_URL wins over
    # LANGFUSE_HOST; both default to the EU cloud).
    host = (
        os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or "https://cloud.langfuse.com"
    )

    if public_key and secret_key:
        _logger.info(
            "langfuse_tracing_active",
            extra={"host": host},
        )
    else:
        missing = [
            k
            for k, v in {
                "LANGFUSE_PUBLIC_KEY": public_key,
                "LANGFUSE_SECRET_KEY": secret_key,
            }.items()
            if not v
        ]
        _logger.info(
            "langfuse_tracing_disabled",
            extra={"hint": (f"Set {' and '.join(missing)} to enable Langfuse trace submission.")},
        )


<<<<<<< HEAD
<<<<<<< HEAD
def get_langfuse_handler() -> object | None:
    """Return a fresh Langfuse CallbackHandler for one agent invocation.
||||||| parent of afadb1c (feat(obs): unify Langfuse traces per agent run with descriptive names)
def get_langfuse_handler() -> object | None:
    """Return a fresh Langfuse CallbackHandler for one DeepAgents invocation.
=======
def get_langfuse_handler(
    *,
    run_id: str | None = None,
    trace_name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> object | None:
||||||| parent of a2f50ef (fix(observability): correct Langfuse trace_name and stop trace merging)
def get_langfuse_handler(
    *,
    run_id: str | None = None,
    trace_name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> object | None:
=======
def get_langfuse_handler() -> object | None:
>>>>>>> a2f50ef (fix(observability): correct Langfuse trace_name and stop trace merging)
    """Return a fresh Langfuse CallbackHandler for one DeepAgents invocation.
>>>>>>> afadb1c (feat(obs): unify Langfuse traces per agent run with descriptive names)

    **Must be called inside an active** ``langfuse_agent_trace()`` **context.**
    When the handler is created inside that context, it automatically inherits
    the active OpenTelemetry span via ``ContextVar`` and nests all LangGraph
    sub-operations (LLM calls, tool calls, chain steps) under the root trace.

    If called outside a ``langfuse_agent_trace()`` context, each LangChain root
    runnable creates its own Langfuse trace — the 36-root-trace problem.

    Trace name and session grouping are controlled via LangChain's
    ``RunnableConfig.metadata`` dict, not via this function.  Callers
    should add the following keys to the config metadata before calling
    ``agent.ainvoke()``:

    - ``"langfuse_trace_name"`` — human-readable label shown in the
      Langfuse UI (e.g. ``"review-openbot_review_responder"``).
    - ``"langfuse_session_id"`` — groups related traces under one session.
      Use the agent ``run_id`` so all invocations for the same logical
      job (including evals that re-run the same sample) appear together
      without merging their individual spans.

    Each call returns a handler with a **fresh** trace ID, ensuring that
    separate eval runs for the same sample produce separate, non-overlapping
    traces in Langfuse.

    Returns ``None`` when:
      - ``langfuse`` is not installed, or
      - ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` are not both set.

    Callers inject the result via::

<<<<<<< HEAD
<<<<<<< HEAD
        with langfuse_agent_trace(name=..., session_id=...):
            callbacks = [h for h in [get_langfuse_handler()] if h is not None]
            config["callbacks"] = callbacks
            await agent.ainvoke(..., config=config)
||||||| parent of afadb1c (feat(obs): unify Langfuse traces per agent run with descriptive names)
        callbacks = [h for h in [get_langfuse_handler()] if h is not None]
        config["callbacks"] = callbacks
=======
        callbacks = [
            h
            for h in [get_langfuse_handler(run_id=..., trace_name=..., metadata=...)]
            if h is not None
        ]
||||||| parent of a2f50ef (fix(observability): correct Langfuse trace_name and stop trace merging)
        callbacks = [
            h
            for h in [get_langfuse_handler(run_id=..., trace_name=..., metadata=...)]
            if h is not None
        ]
=======
        callbacks = [h for h in [get_langfuse_handler()] if h is not None]
>>>>>>> a2f50ef (fix(observability): correct Langfuse trace_name and stop trace merging)
        config["callbacks"] = callbacks
>>>>>>> afadb1c (feat(obs): unify Langfuse traces per agent run with descriptive names)
    """
    import os

    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:
        return None

    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None

<<<<<<< HEAD
<<<<<<< HEAD
    # No trace_context — the handler inherits the current OTel ContextVar
    # set by langfuse_agent_trace().  That context manager calls
    # langfuse.start_as_current_observation() which sets the active span,
    # and CallbackHandler() reads it via get_current_observation_id().
    return CallbackHandler()
||||||| parent of afadb1c (feat(obs): unify Langfuse traces per agent run with descriptive names)
    return CallbackHandler()
=======
    # Build trace_context for unified trace per run_id
    trace_context: dict[str, str] | None = None
    if run_id:
        # Use run_id as seed to generate deterministic trace_id
        # This ensures all operations in the same agent run share one trace
        trace_id = Langfuse.create_trace_id(seed=run_id)
        trace_context = {"trace_id": trace_id}

    # Note: Langfuse CallbackHandler doesn't directly support trace_name
    # The trace name will be derived from the first LangChain run in the trace
    # We use metadata to make traces searchable and filterable
    if metadata and trace_context is not None:
        # Merge metadata into trace_context (Langfuse will attach these to the trace)
        trace_context["metadata"] = metadata  # type: ignore[assignment]

    return CallbackHandler(trace_context=trace_context)
>>>>>>> afadb1c (feat(obs): unify Langfuse traces per agent run with descriptive names)


@contextlib.contextmanager
def langfuse_agent_trace(
    *,
    name: str,
    session_id: str = "",
) -> contextlib.AbstractContextManager[object]:
    """Sync context manager that creates a Langfuse root agent trace.

    **This is the canonical entry-point for Langfuse tracing.** It must wrap
    every agent invocation so that all LangGraph sub-operations (LLM calls,
    tool calls, chain steps) nest under one unified trace rather than
    scattering as 30-40 separate root traces.

    How it works (per Langfuse docs):

      1. ``propagate_attributes()`` attaches ``trace_name`` and ``session_id``
         at the OTel baggage level so they propagate to every child span.
      2. ``langfuse.start_as_current_observation(as_type="agent")`` creates the
         root agent span and sets it as the *current* OTel span via ContextVar.
      3. ``get_langfuse_handler()`` called inside this context reads the current
         ContextVar and makes the handler a child of this root span.
      4. All subsequent LangGraph callbacks inherit the parent via OTel context
         propagation within the same asyncio task.

    Uses ``with`` (sync context manager) in async code — valid in Python because
    the OTel ContextVar propagates correctly across ``await`` in the same task.

    No-ops (yields ``None``) when Langfuse is not installed or not configured.

    Usage::

        with langfuse_agent_trace(name="review-review", session_id=run_id):
            handler = get_langfuse_handler()  # inherits root trace
            await agent.ainvoke(  # all spans nest under root
                {"messages": [...]},
                config={"callbacks": [h for h in [handler] if h]},
            )
    """
    import os

    try:
        from langfuse import Langfuse, propagate_attributes
    except ImportError:
        yield None
        return

    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        yield None
        return

    lf = Langfuse()
    with (
        propagate_attributes(trace_name=name, session_id=session_id),
        lf.start_as_current_observation(as_type="agent", name=name) as obs,
    ):
        yield obs
||||||| parent of a2f50ef (fix(observability): correct Langfuse trace_name and stop trace merging)
    # Build trace_context for unified trace per run_id
    trace_context: dict[str, str] | None = None
    if run_id:
        # Use run_id as seed to generate deterministic trace_id
        # This ensures all operations in the same agent run share one trace
        trace_id = Langfuse.create_trace_id(seed=run_id)
        trace_context = {"trace_id": trace_id}

    # Note: Langfuse CallbackHandler doesn't directly support trace_name
    # The trace name will be derived from the first LangChain run in the trace
    # We use metadata to make traces searchable and filterable
    if metadata and trace_context is not None:
        # Merge metadata into trace_context (Langfuse will attach these to the trace)
        trace_context["metadata"] = metadata  # type: ignore[assignment]

    return CallbackHandler(trace_context=trace_context)
=======
    # No trace_context: let Langfuse assign a fresh UUID per invocation.
    # trace_name and session_id reach the handler via LangChain metadata keys
    # "langfuse_trace_name" / "langfuse_session_id" (see CallbackHandler source,
    # lines ~351-364: it reads those keys from the LangChain run metadata dict).
    return CallbackHandler()
>>>>>>> a2f50ef (fix(observability): correct Langfuse trace_name and stop trace merging)


def create_langfuse_root_span(
    *,
    name: str,
    run_id: str,
    metadata: dict[str, Any] | None = None,
) -> contextlib.AbstractContextManager[object] | None:
    """Create a root Langfuse span to unify all operations in one trace.

    This returns a sync context manager that should wrap the agent invocation.
    All LangChain/LangGraph operations within this context will become
    child spans of this root span, resulting in a single unified trace
    instead of many scattered traces.

    Returns ``None`` when Langfuse is not configured.

    Usage::

        with create_langfuse_root_span(name="fix-issue", run_id=..., metadata=...) as root:
            if root is not None:
                root.span.update(input=...)
            result = await agent.ainvoke(...)
            if root is not None:
                root.span.update(output=...)
    """
    import os

    try:
        from langfuse import Langfuse
    except ImportError:
        return None

    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None

    lf = Langfuse()

    # Create a deterministic trace_id from run_id
    trace_id = Langfuse.create_trace_id(seed=run_id)

    @contextlib.contextmanager
    def _root_span_context():
        """Context manager that creates a root span and yields it."""
        root_span = lf.start_as_current_observation(
            name=name,
            as_type="agent",
            trace_context={"trace_id": trace_id},
            metadata=metadata,
        )
        with root_span as span:
            yield span

    return _root_span_context()


__all__ = [
    "create_langfuse_root_span",
    "get_langfuse_handler",
    "init_langfuse",
    "init_langsmith",
    "init_sentry",
    "langfuse_agent_trace",
]

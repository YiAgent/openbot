"""Observability init contract tests — Sentry + Langfuse.

Sentry contracts:
  1. ``init_sentry`` with no DSN must be a silent no-op so local ``make
     dev`` and CI runs never need a Sentry project.
  2. ``init_sentry`` with a DSN must complete without raising, and must
     tag the hub with the component so a future webapp / worker error
     burst is distinguishable in the Sentry UI.

<<<<<<< HEAD
Langfuse ``get_langfuse_handler`` contracts:
  3. Returns ``None`` when LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are absent.
  4. Returns a ``CallbackHandler`` (truthy) when both keys are present.
  5. Each call returns a **distinct** handler object (no shared state).
  6. The handler is created with NO ``trace_context`` — nesting is handled by
     ``langfuse_agent_trace()`` via OTel ContextVar, not by constructor args.

Langfuse ``langfuse_agent_trace`` contracts:
  7. Yields ``None`` and is a no-op when keys are absent.
  8. Yields an observation object (truthy) when keys are present.

We mock the SDKs where network calls would otherwise occur — unit tests
must not attempt DNS / TLS to real ingest endpoints.
||||||| parent of a2f50ef (fix(observability): correct Langfuse trace_name and stop trace merging)
We mock the SDK in test (2) because we don't want unit tests to attempt
DNS / TLS to a real Sentry ingest endpoint.
=======
Langfuse ``get_langfuse_handler`` contracts:
  3. Returns ``None`` when LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are absent
     so callers that guard with ``[h for h in [...] if h is not None]`` behave
     correctly in environments without Langfuse credentials.
  4. Returns a ``CallbackHandler`` (truthy) when both keys are present.
  5. Each call returns a **distinct** handler object — handlers must not be
     shared across concurrent requests.
  6. The handler has no forced ``trace_context`` trace_id — each invocation
     gets a fresh trace so repeated eval runs of the same sample don't merge
     into one Langfuse trace.

We mock the SDKs where network calls would otherwise occur — unit tests
must not attempt DNS / TLS to real ingest endpoints.
>>>>>>> a2f50ef (fix(observability): correct Langfuse trace_name and stop trace merging)
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from sentry_sdk.integrations.logging import LoggingIntegration

from openbot.core.settings import Settings
<<<<<<< HEAD
from openbot.infrastructure.observability import (
    get_langfuse_handler,
    init_sentry,
    langfuse_agent_trace,
)
||||||| parent of a2f50ef (fix(observability): correct Langfuse trace_name and stop trace merging)
from openbot.infrastructure.observability import init_sentry
=======
from openbot.infrastructure.observability import get_langfuse_handler, init_sentry
>>>>>>> a2f50ef (fix(observability): correct Langfuse trace_name and stop trace merging)


def test_init_sentry_is_noop_without_dsn() -> None:
    """No DSN configured → must not raise. sentry-sdk treats dsn=None as
    a documented no-op; we keep the contract surfaced as a unit test."""
    settings = Settings(sentry_dsn=None)
    # Must not raise — Heroku eco worker without OPENBOT_SENTRY_DSN
    # must still boot cleanly.
    init_sentry(settings, component="webapp")
    init_sentry(settings, component="worker")


def test_init_sentry_passes_settings_through_when_dsn_set() -> None:
    """DSN set → SDK init is called with the configured DSN, environment,
    traces sample rate, profile session sample rate, and the component tag."""
    settings = Settings(
        sentry_dsn="https://public@example.ingest.sentry.io/12345",  # type: ignore[arg-type]
        environment="production",
        sentry_traces_sample_rate=0.1,
        sentry_profile_session_sample_rate=1.0,
    )

    with (
        patch("sentry_sdk.init") as mock_init,
        patch("sentry_sdk.set_tag") as mock_tag,
    ):
        init_sentry(settings, component="webapp")

    assert mock_init.called
    kwargs = mock_init.call_args.kwargs
    assert kwargs["dsn"] == "https://public@example.ingest.sentry.io/12345"
    assert kwargs["environment"] == "production"
    assert kwargs["traces_sample_rate"] == 0.1
    # profile_session_sample_rate drives continuous profiling (sentry-sdk 2.24+).
    assert kwargs["profile_session_sample_rate"] == 1.0
    # PII off by default — webhook bodies contain repo/actor already
    # captured in audit_log, no need to duplicate into Sentry.
    assert kwargs["send_default_pii"] is False
    mock_tag.assert_called_once_with("component", "webapp")
    # Sentry Logs product must be enabled.
    assert kwargs["enable_logs"] is True
    # LoggingIntegration must be present with WARNING+ level so INFO lines
    # (e.g. http_request_completed) don't flood Sentry Logs.
    integrations = kwargs["integrations"]
    logging_int = next((i for i in integrations if isinstance(i, LoggingIntegration)), None)
    assert logging_int is not None, "LoggingIntegration missing from integrations list"
    # LoggingIntegration stores levels on internal handlers:
    #   _breadcrumb_handler → breadcrumbs threshold (should be WARNING)
    #   _handler            → Sentry event threshold (should be ERROR)
    assert logging_int._breadcrumb_handler.level == logging.WARNING, (
        "LoggingIntegration breadcrumb level must be WARNING to suppress INFO noise in Sentry Logs"
    )
    assert logging_int._handler.level == logging.ERROR, (
        "LoggingIntegration event level must be ERROR to avoid creating Sentry events for warnings"
    )


def test_init_sentry_survives_missing_sdk(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """If sentry-sdk is somehow uninstalled (slim image), init must log
    + return rather than crashing the app boot."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sentry_sdk" or name.startswith("sentry_sdk."):
            raise ImportError(f"mocked: {name}")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Must not raise.
    init_sentry(Settings(), component="webapp")


def test_init_sentry_logs_sentry_initialised(caplog) -> None:  # type: ignore[no-untyped-def]
    """When a DSN is configured, init_sentry must emit a structured
    'sentry_initialised' log line so operators can confirm the SDK
    is active at startup without needing to check the Sentry dashboard."""
    settings = Settings(
        sentry_dsn="https://public@example.ingest.sentry.io/12345",  # type: ignore[arg-type]
    )
    with (
        patch("sentry_sdk.init"),
        patch("sentry_sdk.set_tag"),
        caplog.at_level(logging.INFO, logger="openbot.infrastructure.observability"),
    ):
        init_sentry(settings, component="webapp")

    messages = [r.message for r in caplog.records]
    assert "sentry_initialised" in messages
<<<<<<< HEAD


# ---------------------------------------------------------------------------
# get_langfuse_handler — Langfuse CallbackHandler factory
# ---------------------------------------------------------------------------

_FAKE_PK = "pk-lf-test-public-key"
_FAKE_SK = "sk-lf-test-secret-key"


class _FakeCallbackHandler:
    """Stand-in for langfuse.langchain.CallbackHandler in unit tests."""

    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs


def _fake_langfuse_module(monkeypatch, fake_handler_cls: type = _FakeCallbackHandler) -> None:  # type: ignore[no-untyped-def]
    """Patch langfuse.langchain.CallbackHandler with *fake_handler_cls*."""
    fake_module = MagicMock()
    fake_module.CallbackHandler = fake_handler_cls
    monkeypatch.setitem(
        __import__("sys").modules,
        "langfuse.langchain",
        fake_module,
    )


def test_get_langfuse_handler_returns_none_without_keys(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No credentials → must return None so callers don't attach a broken handler."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    _fake_langfuse_module(monkeypatch)
    assert get_langfuse_handler() is None


def test_get_langfuse_handler_returns_none_with_only_public_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Partial credentials (public key only) → must return None."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", _FAKE_PK)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    _fake_langfuse_module(monkeypatch)
    assert get_langfuse_handler() is None


def test_get_langfuse_handler_returns_handler_with_both_keys(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Both credentials present → must return a CallbackHandler instance."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", _FAKE_PK)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", _FAKE_SK)
    _fake_langfuse_module(monkeypatch)
    handler = get_langfuse_handler()
    assert isinstance(handler, _FakeCallbackHandler)


def test_get_langfuse_handler_returns_distinct_instances(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Each call must return a *new* handler so concurrent requests don't
    share state (mixing spans from different agent runs)."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", _FAKE_PK)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", _FAKE_SK)
    _fake_langfuse_module(monkeypatch)
    h1 = get_langfuse_handler()
    h2 = get_langfuse_handler()
    assert h1 is not h2


def test_get_langfuse_handler_no_trace_context(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Handler must NOT pass ``trace_context`` to CallbackHandler.

    OTel context propagation via ``langfuse_agent_trace()`` is responsible for
    nesting all LangGraph sub-spans under one root trace.  Passing
    ``trace_context`` directly to the handler bypasses this mechanism and
    causes ``ValueError: invalid literal for int() with base 16`` in the
    Langfuse SDK (which parses the trace_id as a hex integer).

    The handler should be created with no arguments so it reads the current
    OTel ContextVar (set by ``langfuse.start_as_current_observation()``) at
    call time.
    """
    captured_kwargs: list[dict] = []

    class _CapturingHandler:
        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.append(dict(kwargs))

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", _FAKE_PK)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", _FAKE_SK)
    _fake_langfuse_module(monkeypatch, fake_handler_cls=_CapturingHandler)

    get_langfuse_handler()

    assert len(captured_kwargs) == 1
    kwargs = captured_kwargs[0]
    # Must NOT inject trace_context — OTel ContextVar (from langfuse_agent_trace)
    # is the correct mechanism for nesting sub-spans under a root trace.
    assert "trace_context" not in kwargs or kwargs.get("trace_context") is None, (
        "get_langfuse_handler() must not pass trace_context; "
        "use langfuse_agent_trace() context manager to nest spans."
    )


# ---------------------------------------------------------------------------
# langfuse_agent_trace — root trace context manager
# ---------------------------------------------------------------------------


def _fake_langfuse_sdk(monkeypatch) -> tuple[MagicMock, MagicMock]:  # type: ignore[no-untyped-def]
    """Patch langfuse.Langfuse and langfuse.propagate_attributes.

    Returns (mock_langfuse_instance, mock_obs) so tests can inspect calls.
    """
    mock_obs = MagicMock()
    mock_obs.__enter__ = MagicMock(return_value=mock_obs)
    mock_obs.__exit__ = MagicMock(return_value=False)

    mock_lf_instance = MagicMock()
    mock_lf_instance.start_as_current_observation.return_value = mock_obs

    mock_lf_cls = MagicMock(return_value=mock_lf_instance)

    mock_propagate = MagicMock()
    mock_propagate_ctx = MagicMock()
    mock_propagate_ctx.__enter__ = MagicMock(return_value=None)
    mock_propagate_ctx.__exit__ = MagicMock(return_value=False)
    mock_propagate.return_value = mock_propagate_ctx

    fake_langfuse_mod = MagicMock()
    fake_langfuse_mod.Langfuse = mock_lf_cls
    fake_langfuse_mod.propagate_attributes = mock_propagate

    monkeypatch.setitem(__import__("sys").modules, "langfuse", fake_langfuse_mod)
    return mock_lf_instance, mock_obs


def test_langfuse_agent_trace_noop_without_keys(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Without credentials the context manager must yield ``None`` and
    must not attempt to contact Langfuse."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    with langfuse_agent_trace(name="test", session_id="s1") as obs:
        assert obs is None


def test_langfuse_agent_trace_yields_observation_with_keys(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """With credentials the context manager must yield a truthy observation
    and call ``start_as_current_observation`` with ``as_type="agent"``."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", _FAKE_PK)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", _FAKE_SK)

    mock_lf, _mock_obs = _fake_langfuse_sdk(monkeypatch)

    with langfuse_agent_trace(name="review-review", session_id="run-123") as obs:
        assert obs is not None

    mock_lf.start_as_current_observation.assert_called_once()
    call_kwargs = mock_lf.start_as_current_observation.call_args.kwargs
    assert call_kwargs["as_type"] == "agent"
    assert call_kwargs["name"] == "review-review"
||||||| parent of a2f50ef (fix(observability): correct Langfuse trace_name and stop trace merging)
=======


# ---------------------------------------------------------------------------
# get_langfuse_handler — Langfuse CallbackHandler factory
# ---------------------------------------------------------------------------

_FAKE_PK = "pk-lf-test-public-key"
_FAKE_SK = "sk-lf-test-secret-key"


class _FakeCallbackHandler:
    """Stand-in for langfuse.langchain.CallbackHandler in unit tests."""

    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs


def _fake_langfuse_module(monkeypatch, fake_handler_cls: type = _FakeCallbackHandler) -> None:  # type: ignore[no-untyped-def]
    """Patch langfuse.langchain.CallbackHandler with *fake_handler_cls*."""
    fake_module = MagicMock()
    fake_module.CallbackHandler = fake_handler_cls
    monkeypatch.setitem(
        __import__("sys").modules,
        "langfuse.langchain",
        fake_module,
    )


def test_get_langfuse_handler_returns_none_without_keys(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No credentials → must return None so callers don't attach a broken handler."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    _fake_langfuse_module(monkeypatch)
    assert get_langfuse_handler() is None


def test_get_langfuse_handler_returns_none_with_only_public_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Partial credentials (public key only) → must return None."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", _FAKE_PK)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    _fake_langfuse_module(monkeypatch)
    assert get_langfuse_handler() is None


def test_get_langfuse_handler_returns_handler_with_both_keys(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Both credentials present → must return a CallbackHandler instance."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", _FAKE_PK)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", _FAKE_SK)
    _fake_langfuse_module(monkeypatch)
    handler = get_langfuse_handler()
    assert isinstance(handler, _FakeCallbackHandler)


def test_get_langfuse_handler_returns_distinct_instances(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Each call must return a *new* handler so concurrent requests don't
    share state (mixing spans from different agent runs)."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", _FAKE_PK)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", _FAKE_SK)
    _fake_langfuse_module(monkeypatch)
    h1 = get_langfuse_handler()
    h2 = get_langfuse_handler()
    assert h1 is not h2


def test_get_langfuse_handler_no_forced_trace_id(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Handler must be constructed without a ``trace_context`` trace_id.

    The old implementation used ``Langfuse.create_trace_id(seed=run_id)``
    to force a deterministic trace_id.  When the same sample_id was reused
    across eval runs, all observations merged into one Langfuse trace —
    making it impossible to distinguish separate eval runs.

    The fix: no ``trace_context`` is passed; Langfuse assigns a fresh UUID
    per invocation.  Trace name and session grouping travel via LangChain
    RunnableConfig metadata keys (``langfuse_trace_name`` /
    ``langfuse_session_id``), which the CallbackHandler reads automatically.
    """
    captured_kwargs: list[dict] = []

    class _CapturingHandler:
        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.append(dict(kwargs))

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", _FAKE_PK)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", _FAKE_SK)
    _fake_langfuse_module(monkeypatch, fake_handler_cls=_CapturingHandler)

    get_langfuse_handler()

    assert len(captured_kwargs) == 1
    kwargs = captured_kwargs[0]
    # Must not inject a forced trace_id — fresh UUID is assigned by Langfuse.
    assert "trace_context" not in kwargs or kwargs.get("trace_context") is None, (
        "get_langfuse_handler() must not pass trace_context to CallbackHandler; "
        "deterministic trace_id causes eval re-runs of the same sample to merge traces."
    )
>>>>>>> a2f50ef (fix(observability): correct Langfuse trace_name and stop trace merging)

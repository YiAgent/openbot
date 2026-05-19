"""Sentry init contract tests.

We don't validate Sentry SDK internals — those are upstream's job. We
validate the two boundaries this codebase actually relies on:

  1. ``init_sentry`` with no DSN must be a silent no-op so local ``make
     dev`` and CI runs never need a Sentry project.
  2. ``init_sentry`` with a DSN must complete without raising, and must
     tag the hub with the component so a future webapp / worker error
     burst is distinguishable in the Sentry UI.

We mock the SDK in test (2) because we don't want unit tests to attempt
DNS / TLS to a real Sentry ingest endpoint.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from openbot.core.settings import Settings
from openbot.infrastructure.observability import init_sentry


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
    traces sample rate, profiles sample rate, and the component tag."""
    settings = Settings(
        sentry_dsn="https://public@example.ingest.sentry.io/12345",  # type: ignore[arg-type]
        environment="production",
        sentry_traces_sample_rate=0.1,
        sentry_profiles_sample_rate=0.1,
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
    # profiles_sample_rate drives Sentry Profiling on sampled transactions.
    assert kwargs["profiles_sample_rate"] == 0.1
    # PII off by default — webhook bodies contain repo/actor already
    # captured in audit_log, no need to duplicate into Sentry.
    assert kwargs["send_default_pii"] is False
    mock_tag.assert_called_once_with("component", "webapp")


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

# Sentry Logs Integration — Design

**Date:** 2026-05-19
**Status:** Approved
**Scope:** Enable Sentry's Logs product (`enable_logs=True`) with WARNING+ level filtering

---

## Context

Sentry SDK 2.35.0 added first-class log capturing via `enable_logs=True`. The project currently uses
`sentry-sdk[fastapi]>=2.18`, which does not have this feature. The existing `LoggingIntegration`
(implicitly active) defaults to `level=INFO`, which would forward every `http_request_completed`
log line to Sentry — too noisy for production.

Startup sequence (both entrypoints are safe):

- **Worker:** `configure_root_logger()` (force=True) → `init_sentry()` — Sentry's handler is added
  after the JSON handler and both survive.
- **API:** uvicorn starts → `lifespan` calls `init_sentry()` — no `configure_root_logger()` runs
  after, so Sentry's handler persists.

---

## Changes

### 1. `pyproject.toml`

Bump the minimum sentry-sdk version:

```
"sentry-sdk[fastapi]>=2.35.0",   # was >=2.18 — Logs feature requires 2.35+
```

No new extras required. `LoggingIntegration` is in the core SDK.

### 2. `openbot/infrastructure/observability.py`

Add `enable_logs=True` and an explicit `LoggingIntegration` to `sentry_sdk.init()`:

```python
import logging as _logging
from sentry_sdk.integrations.logging import LoggingIntegration

sentry_sdk.init(
    dsn=dsn,
    environment=settings.environment,
    traces_sample_rate=settings.sentry_traces_sample_rate,
    profiles_sample_rate=settings.sentry_profiles_sample_rate,
    send_default_pii=False,
    enable_logs=True,                       # Sentry Logs product (2.35+)
    integrations=[
        StarletteIntegration(),
        FastApiIntegration(),
        HttpxIntegration(),
        LoggingIntegration(
            level=_logging.WARNING,         # WARNING+ → breadcrumbs + Sentry Logs
            event_level=_logging.ERROR,     # ERROR+   → Sentry error events
        ),
    ],
)
```

**Why explicit `LoggingIntegration`:** Without it, the SDK defaults to `level=INFO`, forwarding
every `http_request_completed`, `sentry_initialised`, etc. to Sentry Logs — high volume with low
signal. Pinning `WARNING` keeps the Logs feed actionable.

**Why `event_level=ERROR`:** Preserves the existing behavior where unhandled exceptions and
explicit `logger.error()` calls create Sentry error events, not just log entries.

### 3. `tests/infrastructure/test_obs.py`

Add one assertion to `test_init_sentry_passes_settings_through_when_dsn_set` — verify a
`LoggingIntegration` instance is present in the `integrations` kwarg:

```python
from sentry_sdk.integrations.logging import LoggingIntegration

integrations = kwargs["integrations"]
assert any(isinstance(i, LoggingIntegration) for i in integrations)
```

No new test file; the existing 4-test suite already covers the init contract.

---

## Data flow

```
Python logger.warning("...")
    → root logger
        → JSON handler (stdout → Papertrail)          [unchanged]
        → Sentry LoggingIntegration handler
            → breadcrumb (attached to next Sentry event)
            → Sentry Logs (enable_logs=True, level ≥ WARNING)

Python logger.error("...")
    → same path, plus:
        → Sentry error event (event_level=ERROR)
```

---

## Out of scope

- `configure_root_logger()` missing from the API entrypoint (separate concern, tracked separately)
- `OPENBOT_SENTRY_LOGGING_LEVEL` Setting (YAGNI — WARNING is correct for all environments)
- Sentry alert rules for specific log patterns (managed in the Sentry dashboard)

---

## Acceptance criteria

1. `pyproject.toml` pins `sentry-sdk[fastapi]>=2.35.0`.
2. `sentry_sdk.init()` in `init_sentry()` includes `enable_logs=True` and `LoggingIntegration(level=WARNING, event_level=ERROR)`.
3. `test_init_sentry_passes_settings_through_when_dsn_set` asserts `LoggingIntegration` is in `integrations`.
4. `make check` passes (all existing tests green, new assertion green).
5. `uv sync` completes without error (sentry-sdk 2.35+ available).

# Sentry Continuous Profiling — Design

**Date:** 2026-05-19
**Status:** Approved
**Scope:** Replace transaction profiling (`profiles_sample_rate`) with continuous profiling (`profile_session_sample_rate` + `start_profiler()`)

---

## Context

`sentry-sdk 2.24.1` introduced continuous profiling via `profile_session_sample_rate` and
`sentry_sdk.profiler.start_profiler()` / `stop_profiler()`. The project currently uses
`profiles_sample_rate=0.1` (transaction-based profiling), which profiles a fraction of
individual HTTP traces. Continuous profiling is always-on, lower overhead, and better suited
to long-running processes like the OpenBot worker.

The SDK minimum is already `>=2.35.0` (set in the Sentry Logs commit) — no version bump needed.

---

## Changes

### 1. `openbot/core/settings.py`

Rename `sentry_profiles_sample_rate` → `sentry_profile_session_sample_rate`, update default
from `0.1` to `1.0`, and update the description:

```python
sentry_profile_session_sample_rate: float = Field(
    default=1.0,
    ge=0.0,
    le=1.0,
    description="Fraction of sessions profiled continuously (sentry-sdk 2.24+).",
)
```

Default `1.0` is correct for continuous profiling — the overhead is substantially lower than
transaction profiling, and Sentry's own quickstart recommends 1.0 as the starting value.

### 2. `openbot/infrastructure/observability.py`

Swap `profiles_sample_rate` → `profile_session_sample_rate` in `sentry_sdk.init()`. No other
changes. Profiler lifecycle (start/stop) stays in the entrypoints.

```python
sentry_sdk.init(
    dsn=dsn,
    environment=settings.environment,
    traces_sample_rate=settings.sentry_traces_sample_rate,
    profile_session_sample_rate=settings.sentry_profile_session_sample_rate,
    send_default_pii=False,
    enable_logs=True,
    integrations=[
        StarletteIntegration(),
        FastApiIntegration(),
        HttpxIntegration(),
        LoggingIntegration(level=logging.WARNING, event_level=logging.ERROR),
    ],
)
```

### 3. `openbot/entrypoints/api/app.py`

Call `start_profiler()` immediately after `init_sentry()` in the `lifespan` startup block.
Call `stop_profiler()` in the `finally` cleanup block (before existing resource teardown).
Both calls wrapped in `try/except Exception: pass` for graceful degradation when
`sentry_sdk` is absent or DSN is unset.

```python
# startup (after init_sentry / init_langsmith)
try:
    import sentry_sdk
    sentry_sdk.profiler.start_profiler()
except Exception:
    pass  # SDK absent or DSN=None no-op — profiling is opt-in

# shutdown (first line of finally block)
try:
    import sentry_sdk
    sentry_sdk.profiler.stop_profiler()
except Exception:
    pass
```

### 4. `openbot/entrypoints/worker/__main__.py`

Call `start_profiler()` immediately after `init_sentry()` in `_main`. No `stop_profiler()`
call — continuous profiling of a long-running process is the intended use case; the SDK
flushes and stops cleanly on process exit (SIGTERM → `asyncio.run()` returns).

```python
# after init_sentry / init_langsmith
try:
    import sentry_sdk
    sentry_sdk.profiler.start_profiler()
except Exception:
    pass
```

### 5. `tests/infrastructure/test_obs.py`

In `test_init_sentry_passes_settings_through_when_dsn_set`, swap the profiles assertion:

```python
# Settings constructor — replace:
sentry_profiles_sample_rate=0.1
# with:
sentry_profile_session_sample_rate=1.0

# Assertion — replace:
assert kwargs["profiles_sample_rate"] == 0.1
# with:
assert kwargs["profile_session_sample_rate"] == 1.0
```

No new test file. The `except Exception: pass` paths in the entrypoints are covered
implicitly by the existing `test_init_sentry_survives_missing_sdk` pattern.

### 6. Doppler `prd` (post-implementation)

```bash
doppler secrets delete OPENBOT_SENTRY_PROFILES_SAMPLE_RATE --project openbot --config prd
doppler secrets set OPENBOT_SENTRY_PROFILE_SESSION_SAMPLE_RATE=1.0 --project openbot --config prd
```

---

## Data flow

```
Process starts
  └─ init_sentry()          # configures SDK with profile_session_sample_rate=1.0
  └─ start_profiler()       # begins continuous profiling session

Process runs
  └─ profiler samples stack traces every ~10ms → buffered in SDK
  └─ SDK flushes profile data to Sentry ingest on interval

webapp: SIGTERM → lifespan finally
  └─ stop_profiler()        # flush remaining profile data
  └─ adapter/redis/db teardown

worker: SIGTERM → shutdown event → asyncio.run() returns
  └─ SDK atexit hook flushes remaining profile data (no explicit stop needed)
```

---

## Out of scope

- `OPENBOT_SENTRY_PROFILING_ENABLED` feature flag (YAGNI — DSN=None is already the off switch)
- `stop_profiler()` in the worker (SDK handles it on process exit)
- Transaction profiling (`profiles_sample_rate`) as a fallback or alias

---

## Acceptance criteria

1. `sentry_profiles_sample_rate` removed from `Settings`; `sentry_profile_session_sample_rate` added with default `1.0`.
2. `sentry_sdk.init()` passes `profile_session_sample_rate` (not `profiles_sample_rate`).
3. `start_profiler()` called in `lifespan` startup and `_main` after `init_sentry()`.
4. `stop_profiler()` called in `lifespan` finally block.
5. Both profiler calls wrapped in `try/except Exception: pass`.
6. `test_init_sentry_passes_settings_through_when_dsn_set` asserts `profile_session_sample_rate == 1.0`.
7. `make check` passes — 697+ tests green, ruff clean.
8. Doppler `prd` updated: old key deleted, new key set to `1.0`.

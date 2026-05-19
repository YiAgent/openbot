# Sentry DSN + Metrics Audit — Design

**Date:** 2026-05-19  
**Status:** Approved  
**Scope:** Wire up the live Sentry DSN and add unit-test coverage for the metrics shim

---

## Context

OpenBot already has a fully-built Sentry integration:

| File | Role |
|---|---|
| `openbot/infrastructure/observability.py` | `init_sentry()` — FastAPI + Starlette + HTTPX integrations |
| `openbot/core/sentry_metrics.py` | `Metrics` shim — `incr/distribution/gauge` wrappers |
| `openbot/core/settings.py` | `sentry_dsn`, `sentry_traces_sample_rate`, `sentry_profiles_sample_rate` |
| `.env` | `OPENBOT_SENTRY_DSN=` placeholder (currently empty) |

The only missing piece is the live DSN. A pre-implementation audit also found one dead-code issue.

---

## Audit Findings

### API compatibility (sentry-sdk ≥ 2.18)

The installed `sentry_sdk.metrics` API surface:

```
count(name, value, unit=None, attributes=None)
distribution(name, value, unit=None, attributes=None)
gauge(name, value, unit=None, attributes=None)
time(name, value, unit=None, attributes=None)
```

Our shim calls match exactly (✅ no changes needed to call sites).

### Dead code: `Metrics.set()`

`sentry_sdk.metrics` has no `set` function in 2.x. The existing guard:

```python
if hasattr(metrics, "set"):
    metrics.set(...)
```

…always skips, meaning any caller of `Metrics.set()` silently loses data with no error.

**Fix:** Remove `Metrics.set()` and add a docstring note explaining the 2.x API dropped the set metric type.

---

## Changes

### 1. Configure DSN — `.env`

Uncomment and populate:

```
OPENBOT_SENTRY_DSN=https://f07a401fd38b70db0359272557c974d6@o4511407339012096.ingest.us.sentry.io/4511407339143168
```

No other config files change. `Settings.sentry_dsn` reads this via the `OPENBOT_` prefix and wraps it in `SecretStr` (never logged, never in repr).

### 2. Remove dead method — `openbot/core/sentry_metrics.py`

- Delete the `Metrics.set()` method.
- Add a short docstring note: "The sentry-sdk 2.x metrics API does not expose a set metric type; use `distribution()` for unique-count approximations."

### 3. New test file — `tests/core/test_sentry_metrics.py`

Patch `sentry_sdk.metrics` and assert:

| Test | Assertion |
|---|---|
| `test_incr_calls_count` | `Metrics.incr("k", 2.0, tags={"a":"b"})` → `metrics.count("k", 2.0, unit="none", attributes={"a":"b"})` |
| `test_distribution_calls_sdk` | `Metrics.distribution("lat", 12.3, unit="millisecond")` → `metrics.distribution("lat", 12.3, unit="millisecond", attributes={})` |
| `test_gauge_calls_sdk` | `Metrics.gauge("q", 7.0)` → `metrics.gauge("q", 7.0, unit="none", attributes={})` |
| `test_incr_swallows_attribute_error` | `AttributeError` on the SDK call → no exception raised |
| `test_incr_swallows_runtime_error` | `RuntimeError` on the SDK call → no exception raised |
| `test_incr_swallows_import_error` | `ImportError` on SDK import → no exception raised |

### 4. New test file — `tests/infrastructure/test_observability.py`

Patch `sentry_sdk.init` and assert:

| Test | Assertion |
|---|---|
| `test_init_sentry_with_dsn` | `sentry_sdk.init` called with `dsn=<value>`, `environment`, `traces_sample_rate`, `profiles_sample_rate`, `send_default_pii=False`, and 3 integrations |
| `test_init_sentry_no_dsn` | `sentry_sdk.init` called with `dsn=None` (documented no-op) |
| `test_init_sentry_logs_when_dsn_present` | `caplog` captures `sentry_initialised` log line |
| `test_init_sentry_no_log_when_no_dsn` | No `sentry_initialised` log line when DSN is absent |

---

## Data flow (unchanged)

```
FastAPI request
    → StarletteIntegration (auto-captures ASGI exceptions)
    → FastApiIntegration   (route-level spans)
    → HttpxIntegration     (GitHub API call latency)
    → Metrics.incr/distribution/gauge → sentry_sdk.metrics.count/distribution/gauge
```

---

## Out of scope

- Sentry alert rules / issue grouping (managed in the Sentry dashboard)
- Live integration verification script (requires running server)
- `.env.example` update (placeholder already present)
- Any changes to `traces_sample_rate` or `profiles_sample_rate` defaults (0.1 is the correct production starting point)

---

## Acceptance criteria

1. `OPENBOT_SENTRY_DSN` is set in `.env`.
2. `Metrics.set()` is removed from `sentry_metrics.py`.
3. `tests/core/test_sentry_metrics.py` — all 6 tests pass.
4. `tests/infrastructure/test_observability.py` — all 4 tests pass.
5. `make check` passes (671 + 10 tests, no regressions).

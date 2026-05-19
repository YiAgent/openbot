# Sentry DSN + Metrics Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the live Sentry DSN, remove dead `Metrics.set()` code, and add unit-test coverage for the metrics shim and `init_sentry`.

**Architecture:** `.env` gains the live DSN so `Settings.sentry_dsn` becomes non-None at runtime; `init_sentry()` then passes it to `sentry_sdk.init()`, which is already wired in both the API and worker entrypoints. New tests mock `sentry_sdk.metrics` and `sentry_sdk.init` so no real Sentry traffic is required during CI.

**Tech Stack:** Python 3.12, sentry-sdk ≥ 2.18, pytest, unittest.mock

**Spec:** `docs/superpowers/specs/2026-05-19-sentry-dsn-metrics-audit-design.md`

---

## File map

| Action | Path | Purpose |
|---|---|---|
| Modify | `.env` | Populate `OPENBOT_SENTRY_DSN` |
| Modify | `openbot/core/sentry_metrics.py` | Remove dead `Metrics.set()` |
| Create | `tests/core/__init__.py` | Package marker (mirrors `openbot/core/`) |
| Create | `tests/core/test_sentry_metrics.py` | 6 tests for the metrics shim |
| Modify | `tests/infrastructure/test_obs.py` | 2 additional `init_sentry` assertions |

---

## Task 1: Set the live Sentry DSN

**Files:**
- Modify: `.env`

- [ ] **Step 1: Open `.env` and populate the DSN**

Find the line (currently blank after `=`):
```
# OPENBOT_SENTRY_DSN=
```
Replace it with (uncommented, real DSN):
```
OPENBOT_SENTRY_DSN=https://f07a401fd38b70db0359272557c974d6@o4511407339012096.ingest.us.sentry.io/4511407339143168
```

- [ ] **Step 2: Verify Settings picks it up**

```bash
uv run python -c "
from openbot.core.settings import get_settings
get_settings.cache_clear()
s = get_settings()
print('DSN set:', s.sentry_dsn is not None)
"
```
Expected output:
```
DSN set: True
```

> **Note:** `.env` is gitignored. This step makes no git change — do NOT commit it.

---

## Task 2: Remove dead `Metrics.set()` method

**Files:**
- Modify: `openbot/core/sentry_metrics.py`

- [ ] **Step 1: Write the test that verifies `set()` does NOT exist on `Metrics`**

Create `tests/core/__init__.py` (empty file):
```bash
touch tests/core/__init__.py
```

Create `tests/core/test_sentry_metrics.py` with just this test for now:

```python
"""Unit tests for openbot.core.sentry_metrics.Metrics shim."""
from __future__ import annotations

from openbot.core.sentry_metrics import Metrics


def test_metrics_has_no_set_method() -> None:
    """Metrics.set() was removed because sentry_sdk.metrics has no 'set'
    function in 2.x — callers were silently dropping data with no error."""
    assert not hasattr(Metrics(), "set")
```

- [ ] **Step 2: Run the test — it should FAIL (set() still exists)**

```bash
uv run pytest tests/core/test_sentry_metrics.py::test_metrics_has_no_set_method -v
```
Expected: **FAILED** — `AssertionError` because `Metrics.set` currently exists.

- [ ] **Step 3: Remove `Metrics.set()` and update the module docstring**

In `openbot/core/sentry_metrics.py`, delete the entire `set()` method (lines ~70–84):

```python
    def set(
        self,
        key: str,
        value: Any,
        unit: str = "none",
        tags: dict[str, Any] | None = None,
    ) -> None:
        """Record a set (unique counts, e.g. user_id)."""
        try:
            from sentry_sdk import metrics

            # If 'set' is not available, we skip it.
            if hasattr(metrics, "set"):
                metrics.set(key, value, unit=unit, attributes=tags or {})
        except (ImportError, RuntimeError, AttributeError):
            pass
```

Also update the module-level docstring. Replace the example block:

```python
Example::

    from openbot.infrastructure.metrics import metrics

    metrics.incr("workflow_started", tags={"type": "triage"})

If Sentry is not initialised (e.g. local dev without DSN), these calls
become no-ops.
```

with:

```python
Example::

    from openbot.core.sentry_metrics import metrics

    metrics.incr("workflow_started", tags={"type": "triage"})
    metrics.distribution("llm_latency_ms", 340.0, unit="millisecond")
    metrics.gauge("queue_depth", 7.0)

If Sentry is not initialised (e.g. local dev without DSN), these calls
become silent no-ops.

Note: sentry_sdk ≥ 2.x dropped the ``set`` metric type from its public
API. Use ``distribution()`` for approximate unique-count approximations.
"""
```

- [ ] **Step 4: Run the test — it should now PASS**

```bash
uv run pytest tests/core/test_sentry_metrics.py::test_metrics_has_no_set_method -v
```
Expected: **PASSED**

- [ ] **Step 5: Commit**

```bash
git add tests/core/__init__.py tests/core/test_sentry_metrics.py openbot/core/sentry_metrics.py
git commit -m "fix: remove dead Metrics.set() — sentry_sdk 2.x has no set metric type"
```

---

## Task 3: Add happy-path tests for `incr`, `distribution`, `gauge`

**Files:**
- Modify: `tests/core/test_sentry_metrics.py`

The three methods are already correct (confirmed against live sentry_sdk.metrics signatures). These tests lock that contract in so a future SDK upgrade that renames an API immediately turns red.

- [ ] **Step 1: Append the three happy-path tests**

Add to `tests/core/test_sentry_metrics.py` after the existing test:

```python
from unittest.mock import patch


def test_incr_calls_sentry_count() -> None:
    """Metrics.incr() must delegate to sentry_sdk.metrics.count() with
    matching name, value, unit, and attributes."""
    m = Metrics()
    with patch("sentry_sdk.metrics") as mock_sdk_metrics:
        m.incr("workflow.started", 2.0, tags={"feature": "triage"})
    mock_sdk_metrics.count.assert_called_once_with(
        "workflow.started",
        2.0,
        unit="none",
        attributes={"feature": "triage"},
    )


def test_distribution_calls_sentry_distribution() -> None:
    """Metrics.distribution() must delegate to sentry_sdk.metrics.distribution()."""
    m = Metrics()
    with patch("sentry_sdk.metrics") as mock_sdk_metrics:
        m.distribution("http.request.duration", 12.3, unit="millisecond")
    mock_sdk_metrics.distribution.assert_called_once_with(
        "http.request.duration",
        12.3,
        unit="millisecond",
        attributes={},
    )


def test_gauge_calls_sentry_gauge() -> None:
    """Metrics.gauge() must delegate to sentry_sdk.metrics.gauge()."""
    m = Metrics()
    with patch("sentry_sdk.metrics") as mock_sdk_metrics:
        m.gauge("queue_depth", 7.0, tags={"stream": "openbot:workflows"})
    mock_sdk_metrics.gauge.assert_called_once_with(
        "queue_depth",
        7.0,
        unit="none",
        attributes={"stream": "openbot:workflows"},
    )
```

- [ ] **Step 2: Run the three new tests**

```bash
uv run pytest tests/core/test_sentry_metrics.py -k "incr or distribution or gauge" -v
```
Expected: **3 PASSED**

- [ ] **Step 3: Commit**

```bash
git add tests/core/test_sentry_metrics.py
git commit -m "test: happy-path coverage for Metrics.incr/distribution/gauge"
```

---

## Task 4: Add resilience tests (errors are silenced)

**Files:**
- Modify: `tests/core/test_sentry_metrics.py`

- [ ] **Step 1: Append the three resilience tests**

Add to `tests/core/test_sentry_metrics.py`:

```python
import builtins


def test_incr_swallows_attribute_error() -> None:
    """If sentry_sdk is installed but not yet initialised, metrics.count
    raises AttributeError. Metrics.incr() must not propagate it."""
    m = Metrics()
    with patch("sentry_sdk.metrics") as mock_sdk_metrics:
        mock_sdk_metrics.count.side_effect = AttributeError("no active hub")
        m.incr("k")  # must not raise


def test_incr_swallows_runtime_error() -> None:
    """If sentry_sdk raises RuntimeError (e.g. misconfigured hub),
    Metrics.incr() must not propagate it."""
    m = Metrics()
    with patch("sentry_sdk.metrics") as mock_sdk_metrics:
        mock_sdk_metrics.count.side_effect = RuntimeError("Sentry not ready")
        m.incr("k")  # must not raise


def test_incr_swallows_import_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """If sentry_sdk is not installed (slim CI image), Metrics.incr()
    must not raise — same graceful degradation pattern as init_sentry."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sentry_sdk" or name.startswith("sentry_sdk."):
            raise ImportError(f"mocked absent: {name}")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    Metrics().incr("k")  # must not raise
```

- [ ] **Step 2: Run the three resilience tests**

```bash
uv run pytest tests/core/test_sentry_metrics.py -k "swallows" -v
```
Expected: **3 PASSED**

- [ ] **Step 3: Run the full new test file**

```bash
uv run pytest tests/core/test_sentry_metrics.py -v
```
Expected: **7 PASSED** (1 from Task 2 + 3 from Task 3 + 3 from Task 4)

- [ ] **Step 4: Commit**

```bash
git add tests/core/test_sentry_metrics.py
git commit -m "test: resilience coverage for Metrics shim (AttributeError / RuntimeError / ImportError)"
```

---

## Task 5: Augment existing `test_obs.py` with two missing assertions

**Files:**
- Modify: `tests/infrastructure/test_obs.py`

`test_obs.py` already covers the no-DSN no-op, the DSN-passes-through happy path, and the missing-SDK graceful degradation. Two things from the spec are missing: `profiles_sample_rate` in the kwargs check, and the `sentry_initialised` log line.

- [ ] **Step 1: Add `import logging` to the imports block at the top of `test_obs.py`**

The existing imports are:
```python
from __future__ import annotations

from unittest.mock import patch

from openbot.core.settings import Settings
from openbot.infrastructure.observability import init_sentry
```

Replace with (add `import logging` after the stdlib block):
```python
from __future__ import annotations

import logging
from unittest.mock import patch

from openbot.core.settings import Settings
from openbot.infrastructure.observability import init_sentry
```

- [ ] **Step 2: Add `profiles_sample_rate` to the existing test**

In `test_init_sentry_passes_settings_through_when_dsn_set`, add one assertion after the existing `send_default_pii` assertion and extend the `Settings(...)` call with `sentry_profiles_sample_rate=0.1`.

Replace the existing function with:

```python
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
    assert kwargs["profiles_sample_rate"] == 0.1
    assert kwargs["send_default_pii"] is False
    mock_tag.assert_called_once_with("component", "webapp")
```

- [ ] **Step 3: Append the log-line test**

Append to `tests/infrastructure/test_obs.py` (the `import logging` import added in Step 1 is already in scope):

```python
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
```

- [ ] **Step 4: Run the updated obs tests**

```bash
uv run pytest tests/infrastructure/test_obs.py -v
```
Expected: **5 PASSED** (3 original + 1 updated + 1 new)

- [ ] **Step 5: Commit**

```bash
git add tests/infrastructure/test_obs.py
git commit -m "test: add profiles_sample_rate and log-line assertions to test_obs"
```

---

## Task 6: Final verification

**Files:** (read-only verification)

- [ ] **Step 1: Run the full test suite**

```bash
make check
```
Expected: all tests pass (671 existing + 7 new core + 2 new obs assertions = 680 total), `ruff` and `ruff format --check` clean.

- [ ] **Step 2: Confirm Sentry will initialise at startup**

```bash
uv run python -c "
from openbot.core.settings import get_settings
get_settings.cache_clear()
s = get_settings()
from openbot.infrastructure.observability import init_sentry
import logging, sys
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
init_sentry(s, component='smoke-test')
print('ok — check above for sentry_initialised log line')
"
```
Expected: a JSON log line containing `sentry_initialised` (or a plain-text equivalent if python-json-logger is absent in this Python env), then `ok`.

- [ ] **Step 3: Push branch**

```bash
git push -u origin feat/observability-and-infra-updates
```

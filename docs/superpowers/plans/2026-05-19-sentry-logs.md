# Sentry Logs Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable Sentry's Logs product with WARNING+ filtering by adding `enable_logs=True` and an explicit `LoggingIntegration` to `sentry_sdk.init()`.

**Architecture:** Two production file changes — bump the sentry-sdk minimum version in `pyproject.toml` and extend the `integrations` list in `init_sentry()`. One test augmentation adds `enable_logs` and `LoggingIntegration` assertions to the existing `test_init_sentry_passes_settings_through_when_dsn_set` test.

**Tech Stack:** Python 3.12, sentry-sdk ≥ 2.35.0, pytest, unittest.mock

**Spec:** `docs/superpowers/specs/2026-05-19-sentry-logs-design.md`

---

## File map

| Action | Path | Purpose |
|---|---|---|
| Modify | `pyproject.toml` | Bump sentry-sdk minimum to 2.35.0 |
| Modify | `openbot/infrastructure/observability.py` | Add `enable_logs=True` + `LoggingIntegration` |
| Modify | `tests/infrastructure/test_obs.py` | Add `enable_logs` + `LoggingIntegration` assertions |

---

## Task 1: TDD — write failing tests, implement, verify green

**Files:**
- Modify: `tests/infrastructure/test_obs.py`
- Modify: `openbot/infrastructure/observability.py`
- Modify: `pyproject.toml`

### Step 1: Add the import to `test_obs.py`

At the top of `tests/infrastructure/test_obs.py`, the current imports are:

```python
from __future__ import annotations

import logging
from unittest.mock import patch

from openbot.core.settings import Settings
from openbot.infrastructure.observability import init_sentry
```

Add one import after the `from unittest.mock import patch` line:

```python
from __future__ import annotations

import logging
from unittest.mock import patch

from sentry_sdk.integrations.logging import LoggingIntegration

from openbot.core.settings import Settings
from openbot.infrastructure.observability import init_sentry
```

- [ ] **Step 2: Add two failing assertions to the existing test**

In `test_init_sentry_passes_settings_through_when_dsn_set`, after the existing `mock_tag.assert_called_once_with("component", "webapp")` line, append:

```python
    # Sentry Logs product must be enabled.
    assert kwargs["enable_logs"] is True
    # LoggingIntegration must be present with WARNING+ level so INFO lines
    # (e.g. http_request_completed) don't flood Sentry Logs.
    integrations = kwargs["integrations"]
    assert any(isinstance(i, LoggingIntegration) for i in integrations)
```

The complete updated test function for reference:

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
    # Sentry Logs product must be enabled.
    assert kwargs["enable_logs"] is True
    # LoggingIntegration must be present with WARNING+ level so INFO lines
    # (e.g. http_request_completed) don't flood Sentry Logs.
    integrations = kwargs["integrations"]
    assert any(isinstance(i, LoggingIntegration) for i in integrations)
```

- [ ] **Step 3: Run the test — it should FAIL**

```bash
uv run pytest tests/infrastructure/test_obs.py::test_init_sentry_passes_settings_through_when_dsn_set -v
```

Expected: **FAILED** — `KeyError: 'enable_logs'` (or `AssertionError`) because `enable_logs` and `LoggingIntegration` are not yet in the init call.

- [ ] **Step 4: Implement — update `observability.py`**

In `openbot/infrastructure/observability.py`, locate the `try` block inside `init_sentry`:

```python
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.httpx import HttpxIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
```

Replace it with (add `LoggingIntegration` import):

```python
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.httpx import HttpxIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
```

Then locate the `sentry_sdk.init(...)` call:

```python
    sentry_sdk.init(
        dsn=dsn,
        environment=settings.environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        profiles_sample_rate=settings.sentry_profiles_sample_rate,
        send_default_pii=False,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
            HttpxIntegration(),
        ],
    )
```

Replace it with:

```python
    sentry_sdk.init(
        dsn=dsn,
        environment=settings.environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        profiles_sample_rate=settings.sentry_profiles_sample_rate,
        send_default_pii=False,
        enable_logs=True,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
            HttpxIntegration(),
            LoggingIntegration(
                level=logging.WARNING,      # WARNING+ → breadcrumbs + Sentry Logs
                event_level=logging.ERROR,  # ERROR+   → Sentry error events
            ),
        ],
    )
```

Note: `logging` is already imported at the top of `observability.py` (`import logging`) — no new import needed at the module level.

- [ ] **Step 5: Bump sentry-sdk in `pyproject.toml`**

Find:
```
    "sentry-sdk[fastapi]>=2.18",
```

Replace with:
```
    "sentry-sdk[fastapi]>=2.35.0",
```

- [ ] **Step 6: Sync dependencies**

```bash
uv sync
```

Expected: completes without error. Sentry SDK 2.35.x (or later) is installed.

Verify:
```bash
uv run python -c "import sentry_sdk; print(sentry_sdk.VERSION)"
```

Expected: version string `2.35.x` or higher (e.g. `2.35.0`).

- [ ] **Step 7: Run the target test — it should now PASS**

```bash
uv run pytest tests/infrastructure/test_obs.py::test_init_sentry_passes_settings_through_when_dsn_set -v
```

Expected: **PASSED**

- [ ] **Step 8: Run the full obs test suite**

```bash
uv run pytest tests/infrastructure/test_obs.py -v
```

Expected: **4 PASSED** — all four tests green.

- [ ] **Step 9: Run the full suite**

```bash
make check
```

Expected: all tests pass, ruff and import-linter clean.

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml openbot/infrastructure/observability.py tests/infrastructure/test_obs.py
git commit -m "feat: enable Sentry Logs with WARNING+ LoggingIntegration (sentry-sdk>=2.35.0)"
```

---

## Task 2: Push and update PR

- [ ] **Step 1: Push**

```bash
git push origin feat/observability-and-infra-updates
```

- [ ] **Step 2: Update PR description**

```bash
gh pr edit 58 --body "$(gh pr view 58 --json body -q .body)

---

## Sentry Logs (added in follow-up commit)

- Bumped \`sentry-sdk[fastapi]>=2.35.0\` (Logs feature requires 2.35+)
- Added \`enable_logs=True\` to \`sentry_sdk.init()\`
- Added explicit \`LoggingIntegration(level=WARNING, event_level=ERROR)\` — keeps INFO lines (e.g. \`http_request_completed\`) out of Sentry Logs while forwarding warnings and errors
- Test: asserts \`enable_logs=True\` and \`LoggingIntegration\` presence in the existing obs test suite
"
```

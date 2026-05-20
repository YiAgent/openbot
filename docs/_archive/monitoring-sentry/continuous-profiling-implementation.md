# Sentry Continuous Profiling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace transaction-based profiling (`profiles_sample_rate`) with continuous profiling (`profile_session_sample_rate` + `start_profiler()`) in both the webapp and worker entrypoints.

**Architecture:** `sentry_sdk.init()` receives `profile_session_sample_rate` (configured via Settings); the entrypoints own the profiler lifecycle — `start_profiler()` at startup, `stop_profiler()` in the webapp's shutdown block (worker relies on process-exit flush). Both profiler calls are wrapped in `try/except Exception: pass` for graceful degradation.

**Tech Stack:** Python 3.12, sentry-sdk ≥ 2.35.0 (already installed at 2.60.0), pytest, pydantic-settings

**Spec:** `docs/superpowers/specs/2026-05-19-sentry-continuous-profiling-design.md`

---

## File map

| Action | Path | Purpose |
|---|---|---|
| Modify | `openbot/core/settings.py` | Rename field + default 1.0 |
| Modify | `openbot/infrastructure/observability.py` | Swap `profiles_sample_rate` → `profile_session_sample_rate` |
| Modify | `openbot/entrypoints/api/app.py` | `start_profiler()` on startup, `stop_profiler()` in finally |
| Modify | `openbot/entrypoints/worker/__main__.py` | `start_profiler()` after `init_sentry()` |
| Modify | `tests/infrastructure/test_obs.py` | Swap assertion + Settings constructor |

---

## Task 1: TDD — settings + observability

**Files:**
- Modify: `tests/infrastructure/test_obs.py`
- Modify: `openbot/core/settings.py:167-172`
- Modify: `openbot/infrastructure/observability.py:81`

### - [ ] Step 1: Update the test to use the new field — it should FAIL

In `tests/infrastructure/test_obs.py`, find `test_init_sentry_passes_settings_through_when_dsn_set`.

Replace the Settings constructor argument:
```python
# OLD
        sentry_profiles_sample_rate=0.1,
# NEW
        sentry_profile_session_sample_rate=1.0,
```

Replace the comment + assertion:
```python
# OLD
    # profiles_sample_rate drives Sentry Profiling on sampled transactions.
    assert kwargs["profiles_sample_rate"] == 0.1
# NEW
    # profile_session_sample_rate drives continuous profiling (sentry-sdk 2.24+).
    assert kwargs["profile_session_sample_rate"] == 1.0
```

The full updated test function:
```python
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
    assert any(isinstance(i, LoggingIntegration) for i in integrations)
```

### - [ ] Step 2: Run the test — it should FAIL

```bash
uv run pytest tests/infrastructure/test_obs.py::test_init_sentry_passes_settings_through_when_dsn_set -v
```

Expected: **FAILED** — `ValidationError` (unknown field `sentry_profile_session_sample_rate`) or `KeyError: 'profile_session_sample_rate'`.

### - [ ] Step 3: Update `settings.py` — rename field + update default

In `openbot/core/settings.py`, find lines 165-172:

```python
    # Sentry Profiling captures function-level performance data.
    # Requires traces_sample_rate > 0.
    sentry_profiles_sample_rate: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="Fraction of sampled transactions that are also profiled.",
    )
```

Replace with:

```python
    # Continuous profiling — always-on, low overhead, does not require
    # traces_sample_rate > 0. 1.0 = profile every session (recommended).
    sentry_profile_session_sample_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Fraction of sessions profiled continuously (sentry-sdk 2.24+).",
    )
```

### - [ ] Step 4: Update `observability.py` — swap the init kwarg

In `openbot/infrastructure/observability.py`, find line 81:

```python
        profiles_sample_rate=settings.sentry_profiles_sample_rate,
```

Replace with:

```python
        profile_session_sample_rate=settings.sentry_profile_session_sample_rate,
```

Also update the docstring comment block above `sentry_sdk.init()` — find:
```python
        # Webhook bodies may carry repo / actor info that's already in
```
and look at the full init call; it should now read:

```python
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
```

### - [ ] Step 5: Run the target test — it should PASS

```bash
uv run pytest tests/infrastructure/test_obs.py::test_init_sentry_passes_settings_through_when_dsn_set -v
```

Expected: **PASSED**

### - [ ] Step 6: Run the full obs suite

```bash
uv run pytest tests/infrastructure/test_obs.py -v
```

Expected: **4 PASSED**

### - [ ] Step 7: Commit

```bash
git add openbot/core/settings.py openbot/infrastructure/observability.py tests/infrastructure/test_obs.py
git commit -m "feat: switch to continuous profiling (profile_session_sample_rate=1.0)"
```

---

## Task 2: Entrypoint profiler lifecycle

**Files:**
- Modify: `openbot/entrypoints/api/app.py`
- Modify: `openbot/entrypoints/worker/__main__.py`

### - [ ] Step 1: Update webapp `lifespan` — start on startup, stop on shutdown

In `openbot/entrypoints/api/app.py`, find:

```python
    init_sentry(settings, component="webapp")
    init_langsmith()
    auth = _build_auth(settings)
```

Replace with:

```python
    init_sentry(settings, component="webapp")
    init_langsmith()
    try:
        import sentry_sdk as _sentry
        _sentry.profiler.start_profiler()
    except Exception:
        pass  # SDK absent or DSN=None no-op — profiling is opt-in
    auth = _build_auth(settings)
```

Then find the `finally` block:

```python
    try:
        yield
    finally:
        adapter: GitHubAdapter | None = app.state.github_adapter
        if adapter is not None:
            await adapter.aclose()
```

Replace with:

```python
    try:
        yield
    finally:
        try:
            import sentry_sdk as _sentry
            _sentry.profiler.stop_profiler()
        except Exception:
            pass
        adapter: GitHubAdapter | None = app.state.github_adapter
        if adapter is not None:
            await adapter.aclose()
```

### - [ ] Step 2: Update worker `_main` — start only (no stop)

In `openbot/entrypoints/worker/__main__.py`, find:

```python
    init_sentry(settings, component="worker")
    init_langsmith()
    if settings.redis_url is None:
```

Replace with:

```python
    init_sentry(settings, component="worker")
    init_langsmith()
    try:
        import sentry_sdk as _sentry
        _sentry.profiler.start_profiler()
    except Exception:
        pass  # SDK absent or DSN=None no-op — profiling is opt-in
    if settings.redis_url is None:
```

No `stop_profiler()` in the worker — the SDK flushes remaining profile data via its atexit hook when SIGTERM causes `asyncio.run()` to return.

### - [ ] Step 3: Run the full test suite

```bash
make check
```

Expected: **697 passed** (or higher), ruff clean, import-linter clean.

### - [ ] Step 4: Commit

```bash
git add openbot/entrypoints/api/app.py openbot/entrypoints/worker/__main__.py
git commit -m "feat: start continuous profiler in webapp lifespan and worker _main"
```

---

## Task 3: Doppler + push

### - [ ] Step 1: Update Doppler `prd` — swap the env var

```bash
doppler secrets delete OPENBOT_SENTRY_PROFILES_SAMPLE_RATE --project openbot --config prd --yes
doppler secrets set OPENBOT_SENTRY_PROFILE_SESSION_SAMPLE_RATE=1.0 --project openbot --config prd
```

Verify:
```bash
doppler secrets --project openbot --config prd --only-names | grep SENTRY
```

Expected output includes `OPENBOT_SENTRY_PROFILE_SESSION_SAMPLE_RATE` and does NOT include `OPENBOT_SENTRY_PROFILES_SAMPLE_RATE`.

### - [ ] Step 2: Push

```bash
git push origin feat/observability-and-infra-updates
```

### - [ ] Step 3: Update PR #58 description

```bash
gh pr edit 58 --body "$(gh pr view 58 --json body -q .body)

---

## Sentry Continuous Profiling (added in follow-up commit)

- Replaced \`profiles_sample_rate\` (transaction profiling) with \`profile_session_sample_rate=1.0\` (continuous profiling)
- \`sentry_sdk.profiler.start_profiler()\` called in webapp \`lifespan\` startup and worker \`_main\` after \`init_sentry()\`
- \`stop_profiler()\` called in webapp \`lifespan\` finally block; worker relies on SDK atexit flush
- Both calls wrapped in \`try/except Exception: pass\` — graceful degradation when SDK absent or DSN unset
- Doppler \`prd\`: removed \`OPENBOT_SENTRY_PROFILES_SAMPLE_RATE\`, added \`OPENBOT_SENTRY_PROFILE_SESSION_SAMPLE_RATE=1.0\`
"
```

"""Unit tests for allowlist-based trace routing in evals.common.langsmith."""

from __future__ import annotations

import pytest

from evals.common import langsmith as ls


def test_known_public_dataset_returns_true() -> None:
    assert ls.is_public_dataset("martian_2026w20") is True


def test_unknown_dataset_fails_safe_to_internal() -> None:
    """Anything not in the allowlist must default to internal — never accidentally public."""
    assert ls.is_public_dataset("nonexistent_v1") is False
    assert ls.is_public_dataset("internal_prs_v1") is False


def test_configure_tracing_skips_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    state = ls.configure_tracing_for_dataset("martian_2026w20")
    assert state["source"] == "skipped:no-api-key"
    assert state["project"] is None


def test_public_dataset_routes_to_public_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setenv("LANGSMITH_PROJECT_PUBLIC", "openbot-eval-public")
    monkeypatch.setenv("LANGSMITH_PROJECT_INTERNAL", "openbot-eval-internal")
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

    state = ls.configure_tracing_for_dataset("martian_2026w20")

    assert state == {
        "project": "openbot-eval-public",
        "tracing": "true",
        "source": "allowlist-public",
    }


def test_unknown_dataset_routes_to_internal_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setenv("LANGSMITH_PROJECT_PUBLIC", "openbot-eval-public")
    monkeypatch.setenv("LANGSMITH_PROJECT_INTERNAL", "openbot-eval-internal")
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

    state = ls.configure_tracing_for_dataset("unknown_v1")

    assert state["project"] == "openbot-eval-internal"
    assert state["source"] == "allowlist-internal"


def test_configure_tracing_overwrites_preexisting_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doppler's default LANGSMITH_PROJECT must NOT shadow allowlist routing."""
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setenv("LANGSMITH_PROJECT", "openbot-dev")  # default value
    monkeypatch.setenv("LANGSMITH_PROJECT_PUBLIC", "openbot-eval-public")
    monkeypatch.delenv("LANGSMITH_PROJECT_INTERNAL", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

    ls.configure_tracing_for_dataset("martian_2026w20")

    import os

    assert os.environ["LANGSMITH_PROJECT"] == "openbot-eval-public"


def test_configure_tracing_honors_explicit_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-set LANGSMITH_TRACING is left alone (allows muting noisy dev loops)."""
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGSMITH_PROJECT_PUBLIC", "openbot-eval-public")
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

    state = ls.configure_tracing_for_dataset("martian_2026w20")

    assert state["tracing"] == "false"  # untouched

"""Unit tests for single-project trace routing in evals.agents.langsmith."""

from __future__ import annotations

import os

import pytest

from evals.agents import langsmith as ls


def test_configure_tracing_skips_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    state = ls.configure_tracing_for_dataset("martian_2026w20")
    assert state["source"] == "skipped:no-api-key"
    assert state["project"] is None


def test_eval_project_env_drives_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """LANGSMITH_PROJECT_EVAL is the single source of truth for all 4 eval cells."""
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setenv("LANGSMITH_PROJECT_EVAL", "openbot-eval")
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

    for dataset_version in (
        "martian_2026w20",
        "chat_swe_qa_pro_v1",
        "fix_swe_bench_verified",
        "test_swt_bench_verified",
    ):
        state = ls.configure_tracing_for_dataset(dataset_version)
        assert state == {
            "project": "openbot-eval",
            "tracing": "true",
            "source": "eval-project",
        }
        assert os.environ["LANGSMITH_PROJECT"] == "openbot-eval"


def test_configure_tracing_overwrites_preexisting_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doppler's default LANGSMITH_PROJECT must NOT shadow the eval routing."""
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setenv("LANGSMITH_PROJECT", "openbot-dev")  # default value
    monkeypatch.setenv("LANGSMITH_PROJECT_EVAL", "openbot-eval")
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

    ls.configure_tracing_for_dataset("martian_2026w20")

    assert os.environ["LANGSMITH_PROJECT"] == "openbot-eval"


def test_configure_tracing_reports_unset_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """When LANGSMITH_PROJECT_EVAL is missing, tracing still enables but project=None."""
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.delenv("LANGSMITH_PROJECT_EVAL", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

    state = ls.configure_tracing_for_dataset("martian_2026w20")

    assert state["project"] is None
    assert state["source"] == "eval-project:unset"
    assert state["tracing"] == "true"


def test_configure_tracing_honors_explicit_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-set LANGSMITH_TRACING is left alone (allows muting noisy dev loops)."""
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGSMITH_PROJECT_EVAL", "openbot-eval")
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

    state = ls.configure_tracing_for_dataset("martian_2026w20")

    assert state["tracing"] == "false"  # untouched

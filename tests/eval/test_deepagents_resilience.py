"""Contract tests for deepagents HTTP-layer timeout + retry knobs.

These guard the fix for the CLOSE_WAIT-hang we observed on flaky model
endpoints: without an explicit ``timeout=`` the provider httpx client
inherits "no read timeout" and a half-closed socket pins the eval forever.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from evals.common import config
from evals.common.deepagents_baseline import (
    build_chat_model,
    get_model_max_retries,
    get_model_timeout_s,
)

# Documented baseline — the locked surface tests guard against drift.
# Numbers mirror the pydantic Field defaults in
# :class:`evals.common.config.DeepAgentsSettings`.
_BASELINE_TIMEOUT_S = 90
_BASELINE_MAX_RETRIES = 3


def test_default_resilience_matches_documented_baseline() -> None:
    assert get_model_timeout_s() == _BASELINE_TIMEOUT_S
    assert get_model_max_retries() == _BASELINE_MAX_RETRIES


def test_env_overrides_take_effect(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(config.DEEPAGENTS_MODEL_TIMEOUT_S_ENV, "30")
    monkeypatch.setenv(config.DEEPAGENTS_MODEL_MAX_RETRIES_ENV, "5")
    config.get_eval_config.cache_clear()
    assert get_model_timeout_s() == 30
    assert get_model_max_retries() == 5


def test_invalid_env_fails_loud_at_load(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Pydantic ``PositiveInt`` rejects 0 / negatives / non-numeric.

    The prior behavior silently fell back to the default — that hid
    misconfigured Doppler entries. We now fail loud at startup with a
    pydantic ``ValidationError`` so the operator sees the bad value.
    """
    monkeypatch.setenv(config.DEEPAGENTS_MODEL_TIMEOUT_S_ENV, "0")
    config.get_eval_config.cache_clear()
    with pytest.raises(ValidationError):
        get_model_timeout_s()

    monkeypatch.setenv(config.DEEPAGENTS_MODEL_TIMEOUT_S_ENV, "30")
    monkeypatch.setenv(config.DEEPAGENTS_MODEL_MAX_RETRIES_ENV, "not-a-number")
    config.get_eval_config.cache_clear()
    with pytest.raises(ValidationError):
        get_model_max_retries()


def test_build_chat_model_propagates_timeout_and_retries() -> None:
    """The constructed chat model must carry our timeout + max_retries.

    Without this, the http client falls back to httpx's "no read timeout"
    default and CLOSE_WAIT sockets hang the eval.
    """
    model = build_chat_model("anthropic:claude-sonnet-4-6", timeout_s=7, max_retries=4)
    # ChatAnthropic exposes timeout as ``default_request_timeout``; other
    # langchain providers (OpenAI etc.) use ``request_timeout`` / ``timeout``.
    timeout = getattr(
        model,
        "default_request_timeout",
        getattr(model, "request_timeout", getattr(model, "timeout", None)),
    )
    assert timeout == 7.0 or timeout == 7
    assert getattr(model, "max_retries", None) == 4


def test_build_chat_model_uses_env_when_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(config.DEEPAGENTS_MODEL_TIMEOUT_S_ENV, "11")
    monkeypatch.setenv(config.DEEPAGENTS_MODEL_MAX_RETRIES_ENV, "6")
    model = build_chat_model("anthropic:claude-sonnet-4-6")
    timeout = getattr(
        model,
        "default_request_timeout",
        getattr(model, "request_timeout", getattr(model, "timeout", None)),
    )
    assert timeout == 11.0 or timeout == 11
    assert getattr(model, "max_retries", None) == 6

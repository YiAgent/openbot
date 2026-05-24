"""Shared pytest fixtures.

Two responsibilities:
  1. `rsa_private_key_pem` — ephemeral RSA-2048 PEM for GitHub App auth tests.
  2. `_isolate_openbot_env` — AUTOUSE: strip ambient `OPENBOT_*` env vars +
     LangSmith tracing env + chdir to a clean tmp before every test. Without
     this, tests run in a worktree with a real `.env` symlink (developer's
     actual config) and pydantic-settings happily picks up production-shaped
     values, causing non-deterministic behavior between CI and local — and
     between worktrees. The LangSmith scrub also prevents `langsmith.traceable`
     wrappers inside eval solvers (e.g. evals/solvers/swe_qa.py) from
     accidentally publishing unit-test traces to the real project.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(autouse=True)
def _isolate_openbot_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Strip every `OPENBOT_*` env var and chdir to a clean tmp directory.

    Runs around EVERY test. The fixture has no return — tests don't need
    to depend on it, just inherit the clean environment. Tests that want
    specific values use the usual `monkeypatch.setenv(...)` after this
    fixture has scrubbed the ambient state.

    Why both env and cwd?
      - env  → pydantic-settings reads from os.environ first
      - cwd  → pydantic-settings reads `./.env` second (when env not set)

    The worktree symlinks `.env` from the main worktree, so without chdir
    we'd inherit real GitHub App credentials in every test.
    """
    for key in list(os.environ):
        if key.startswith("OPENBOT_"):
            monkeypatch.delenv(key, raising=False)
    for key in (
        "LANGSMITH_API_KEY",
        "LANGCHAIN_API_KEY",
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING_V2",
        "LANGSMITH_PROJECT",
        "LANGCHAIN_PROJECT",
        "LANGSMITH_PROJECT_EVAL",
        # LLM credentials — strip so tests don't accidentally call real APIs.
        # These use standard SDK env-var names (not OPENBOT_ prefix) and leak
        # from developer shells into test processes, bypassing the OPENBOT_*
        # sweep above. The classifier and runtime read settings.anthropic_api_base
        # (alias: ANTHROPIC_BASE_URL); without stripping this, tests that
        # exercise classify_event hit the real proxy instead of the mock.
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.chdir(tmp_path)
    # Eval settings are cached via lru_cache for production efficiency
    # (env reads are stable for the life of an eval run). Tests mutate
    # env via monkeypatch.setenv mid-test, so we clear before AND after
    # to guarantee no cross-test pollution either direction. Mirrors
    # the openbot.core.settings.get_settings.cache_clear() pattern.
    try:
        from evals.runtime.config import get_eval_config

        get_eval_config.cache_clear()
        yield
        get_eval_config.cache_clear()
    except ImportError:
        # Some unit tests (e.g. pure webapp tests) don't pull in pydantic
        # extras the eval modules need — let them run untouched.
        yield


@pytest.fixture(scope="session")
def rsa_private_key_pem() -> bytes:
    """An ephemeral RSA-2048 PEM, generated once per test session.

    Used to stand in for a GitHub App's private key. Never persisted to disk —
    keeps trufflehog and CI logs clean.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

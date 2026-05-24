"""Root conftest — ambient-env isolation + RSA key for GitHub App tests.

Spec: docs/superpowers/specs/2026-05-24-tests-rebuild-design.md §8.1.

Two responsibilities, no business logic. Per-layer fixtures live in
each layer's own conftest.py.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_SCRUB_PREFIXES: tuple[str, ...] = ("OPENBOT_",)
_SCRUB_EXACT: tuple[str, ...] = (
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
    "LANGSMITH_TRACING",
    "LANGCHAIN_TRACING_V2",
    "LANGSMITH_PROJECT",
    "LANGCHAIN_PROJECT",
    "LANGSMITH_PROJECT_EVAL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def _isolate_ambient_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Scrub OPENBOT_* + LangSmith + LLM creds, chdir to clean tmp.

    Runs around EVERY test. Returns nothing — tests inherit the clean
    environment by virtue of the fixture being autouse. Tests that want
    specific values call ``monkeypatch.setenv(...)`` after this fixture
    has run.
    """
    for key in list(os.environ):
        if any(key.startswith(p) for p in _SCRUB_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    for key in _SCRUB_EXACT:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture(scope="session")
def rsa_private_key_pem() -> bytes:
    """Ephemeral RSA-2048 PEM for GitHub App auth tests.

    Session-scoped because keygen is ~100 ms; safe to share since
    tests treat it as read-only material.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

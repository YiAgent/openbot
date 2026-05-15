"""Shared pytest fixtures.

Two responsibilities:
  1. `rsa_private_key_pem` — ephemeral RSA-2048 PEM for GitHub App auth tests.
  2. `_isolate_openbot_env` — AUTOUSE: strip ambient `OPENBOT_*` env vars +
     chdir to a clean tmp before every test. Without this, tests run in a
     worktree with a real `.env` symlink (developer's actual config) and
     pydantic-settings happily picks up production-shaped values, causing
     non-deterministic behavior between CI and local — and between worktrees.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(autouse=True)
def _isolate_openbot_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    monkeypatch.chdir(tmp_path)


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

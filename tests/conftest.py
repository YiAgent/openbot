"""Shared pytest fixtures."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


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

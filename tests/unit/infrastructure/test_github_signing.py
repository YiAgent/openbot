"""GitHub webhook signature verification — HMAC SHA-256."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from openbot.infrastructure.adapters.base import SignatureError
from openbot.infrastructure.adapters.github import verify_webhook_signature

_SECRET = b"super-secret-shared-key"


def _sign(body: bytes) -> str:
    sig = hmac.new(_SECRET, body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def test_valid_signature_passes() -> None:
    body = b'{"hello":"world"}'
    verify_webhook_signature(body, _sign(body), secret=_SECRET)


def test_tampered_body_fails() -> None:
    body = b'{"hello":"world"}'
    with pytest.raises(SignatureError):
        verify_webhook_signature(b"tampered", _sign(body), secret=_SECRET)


def test_missing_signature_header_fails() -> None:
    with pytest.raises(SignatureError):
        verify_webhook_signature(b"x", "", secret=_SECRET)


def test_malformed_prefix_fails() -> None:
    with pytest.raises(SignatureError):
        verify_webhook_signature(b"x", "md5=abc123", secret=_SECRET)

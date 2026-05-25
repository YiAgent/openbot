"""Real-service tests for GitHub webhook signature verification.

Skipped when OPENBOT_GITHUB_WEBHOOK_SECRET is absent.

These tests exercise the real GitHubAdapter.verify_signature path with
an actual HMAC computation — verifying the integration between
gidgethub and our wrapper, not mocked.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from tests.real_service.conftest import _env_or_skip

# Module-level skip: one 's' per file, not one per test
env = _env_or_skip("OPENBOT_GITHUB_WEBHOOK_SECRET")


def _sign(body: bytes, secret: str) -> str:
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


@pytest.mark.real_service
class TestRealGitHubSignature:
    def test_valid_signature_does_not_raise(self) -> None:
        """Correct HMAC-SHA256 signature passes verify_signature."""
        from openbot.infrastructure.adapters.github import GitHubAdapter

        secret = env["OPENBOT_GITHUB_WEBHOOK_SECRET"]
        adapter = GitHubAdapter(webhook_secret=secret)
        body = json.dumps({"action": "opened"}).encode()
        headers = {
            "x-hub-signature-256": _sign(body, secret),
            "x-github-event": "issues",
            "x-github-delivery": "rs-001",
        }
        # Should not raise
        adapter.verify_signature(body, headers)

    def test_wrong_signature_raises_signature_error(self) -> None:
        """Wrong signature raises SignatureError."""
        from openbot.infrastructure.adapters.base import SignatureError
        from openbot.infrastructure.adapters.github import GitHubAdapter

        secret = env["OPENBOT_GITHUB_WEBHOOK_SECRET"]
        adapter = GitHubAdapter(webhook_secret=secret)
        body = json.dumps({"action": "opened"}).encode()
        headers = {
            "x-hub-signature-256": "sha256=" + "0" * 64,
            "x-github-event": "issues",
            "x-github-delivery": "rs-002",
        }
        with pytest.raises(SignatureError):
            adapter.verify_signature(body, headers)

    def test_missing_signature_raises_signature_error(self) -> None:
        """Missing signature header raises SignatureError."""
        from openbot.infrastructure.adapters.base import SignatureError
        from openbot.infrastructure.adapters.github import GitHubAdapter

        secret = env["OPENBOT_GITHUB_WEBHOOK_SECRET"]
        adapter = GitHubAdapter(webhook_secret=secret)
        body = json.dumps({"action": "opened"}).encode()
        headers = {
            "x-github-event": "issues",
            "x-github-delivery": "rs-003",
            # No x-hub-signature-256
        }
        with pytest.raises(SignatureError):
            adapter.verify_signature(body, headers)

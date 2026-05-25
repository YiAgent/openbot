"""Full-stack E2E assembler — single-process, no real network.

Wires the complete pipeline:
  webhook → queue → use case → channel adapter

All adapters are fakes; the FastAPI app is the real one with
app.state populated by this assembler.  No external network.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any

from openbot.entrypoints.api.app import app as _base_app
from openbot.infrastructure.adapters.github import GitHubAdapter
from openbot.testing.fakes import (
    FakeCancellation,
    FakeDedup,
    FakeQueue,
    FakeResourceLock,
    FakeRunsRepo,
)

_E2E_WEBHOOK_SECRET = "e2e-test-secret"


def sign_payload(body: bytes, secret: str = _E2E_WEBHOOK_SECRET) -> str:
    """Return the ``sha256=<hex>`` HMAC for ``body`` signed with ``secret``."""
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


@dataclass
class E2EStack:
    """Fully wired single-process stack for E2E tests.

    Fields expose the injected fakes so tests can assert on recorded calls.
    """

    dedup: FakeDedup = field(default_factory=FakeDedup)
    queue: FakeQueue = field(default_factory=FakeQueue)
    cancellation: FakeCancellation = field(default_factory=FakeCancellation)
    resource_lock: FakeResourceLock = field(default_factory=FakeResourceLock)
    runs_repo: FakeRunsRepo = field(default_factory=FakeRunsRepo)
    webhook_secret: str = _E2E_WEBHOOK_SECRET

    def sign(self, body: bytes) -> str:
        """Sign a payload with the stack's webhook secret."""
        return sign_payload(body, self.webhook_secret)

    def issue_opened_headers(self, body: bytes, *, delivery_id: str = "e2e-001") -> dict[str, str]:
        """Build headers for a signed issue.opened webhook."""
        return {
            "x-github-event": "issues",
            "x-github-delivery": delivery_id,
            "x-hub-signature-256": self.sign(body),
            "content-type": "application/json",
        }


async def build_e2e_stack() -> E2EStack:
    """Assemble and return a fresh E2EStack.

    Injects fakes into the FastAPI app.state so the real webhook route
    and ingest_webhook use-case use them instead of real infrastructure.
    """
    from openbot.core.settings import get_settings

    get_settings.cache_clear()

    stack = E2EStack()

    # Wire the fakes into the live app.state
    _base_app.state.github_adapter = GitHubAdapter(webhook_secret=stack.webhook_secret)
    _base_app.state.dedup = stack.dedup
    _base_app.state.queue = stack.queue
    _base_app.state.redis = object()  # non-None sentinel — enables queue path
    _base_app.state.cancellation = stack.cancellation
    _base_app.state.resource_lock = stack.resource_lock
    _base_app.state.runs_repo = stack.runs_repo

    return stack


def make_issue_opened_payload(
    *,
    issue_number: int = 1,
    action: str = "opened",
    repo: str = "owner/repo",
    sender_login: str = "octocat",
) -> dict[str, Any]:
    """Minimal issue.opened GitHub payload."""
    return {
        "action": action,
        "issue": {
            "number": issue_number,
            "title": "Test issue",
            "body": "Please help",
            "user": {"login": sender_login, "type": "User"},
            "labels": [],
            "updated_at": "2024-01-01T00:00:00Z",
        },
        "repository": {
            "full_name": repo,
            "owner": {"login": repo.split("/")[0], "type": "Organization"},
        },
        "sender": {"login": sender_login, "type": "User"},
        "installation": {"id": 42},
    }


__all__ = [
    "E2EStack",
    "build_e2e_stack",
    "make_issue_opened_payload",
    "sign_payload",
]

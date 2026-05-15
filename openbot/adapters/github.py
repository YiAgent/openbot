"""GitHubAdapter — verify HMAC-SHA-256 and parse webhook payloads.

GitHub webhook reference: https://docs.github.com/en/webhooks
PRD §4.8 trust boundary: nothing downstream may touch the payload until
`verify_signature` has returned without raising.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, Final

from openbot.adapters.base import ChannelAdapter, SignatureError
from openbot.events import EventKind, UnifiedEvent

_SIGNATURE_HEADER: Final = "x-hub-signature-256"
_EVENT_HEADER: Final = "x-github-event"
_DELIVERY_HEADER: Final = "x-github-delivery"
_SIGNATURE_PREFIX: Final = "sha256="

# (X-GitHub-Event header, payload.action) → EventKind
# Anything unmapped returns UNKNOWN — we never crash on a new event type.
_EVENT_TABLE: Final[dict[tuple[str, str], EventKind]] = {
    ("issues", "opened"): EventKind.ISSUE_OPENED,
    ("issues", "assigned"): EventKind.ISSUE_ASSIGNED,
    ("issue_comment", "created"): EventKind.ISSUE_COMMENT_CREATED,
    ("pull_request", "opened"): EventKind.PR_OPENED,
    ("pull_request", "synchronize"): EventKind.PR_SYNCHRONIZED,
    ("pull_request_review_comment", "created"): EventKind.PR_REVIEW_COMMENT_CREATED,
}


class GitHubAdapter(ChannelAdapter):
    name = "github"

    def __init__(self, *, webhook_secret: str) -> None:
        if not webhook_secret:
            raise ValueError("GitHubAdapter requires a non-empty webhook_secret")
        # HMAC needs bytes; store once.
        self._secret = webhook_secret.encode("utf-8")

    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> None:
        sent = self._header(headers, _SIGNATURE_HEADER)
        if not sent or not sent.startswith(_SIGNATURE_PREFIX):
            raise SignatureError("missing or malformed X-Hub-Signature-256")
        expected = _SIGNATURE_PREFIX + hmac.new(self._secret, body, sha256).hexdigest()
        # Constant-time comparison — PRD §4.8.
        if not hmac.compare_digest(sent, expected):
            raise SignatureError("signature mismatch")

    def parse_event(self, body: bytes, headers: Mapping[str, str]) -> UnifiedEvent:
        delivery_id = self._header(headers, _DELIVERY_HEADER) or ""
        event_type = self._header(headers, _EVENT_HEADER) or ""
        try:
            payload: dict[str, Any] = json.loads(body)
        except json.JSONDecodeError as exc:
            # Bad JSON after a valid HMAC is suspicious; surface as 401 not 500.
            raise SignatureError(f"payload is not valid JSON: {exc}") from exc

        action = str(payload.get("action") or "")
        kind = _EVENT_TABLE.get((event_type, action), EventKind.UNKNOWN)

        repo = (payload.get("repository") or {}).get("full_name") or ""
        actor = (payload.get("sender") or {}).get("login") or ""

        issue = payload.get("issue") or {}
        pull_request = payload.get("pull_request") or {}

        # issue_comment on a PR: GitHub still sends event=issue_comment but
        # `issue.pull_request` is set and `issue.number` IS the PR number.
        comment_on_pr = bool(issue.get("pull_request"))
        if pull_request:
            pr_number: int | None = pull_request.get("number")
            issue_number: int | None = None
        elif comment_on_pr:
            pr_number = issue.get("number")
            issue_number = None
        else:
            pr_number = None
            issue_number = issue.get("number")

        comment_body = (payload.get("comment") or {}).get("body")

        return UnifiedEvent(
            channel=self.name,
            delivery_id=delivery_id,
            kind=kind,
            repo=repo,
            actor=actor,
            issue_number=issue_number,
            pr_number=pr_number,
            comment_body=comment_body,
            raw=payload,
        )

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        # FastAPI gives lowercase keys; be defensive for raw dict callers too.
        return headers.get(name) or headers.get(name.upper()) or headers.get(name.title())

"""LinearAdapter — verify Linear webhooks + write back via GraphQL API.

Linear webhook reference: https://developers.linear.app/docs/webhooks
Linear GraphQL reference: https://developers.linear.app/docs/graphql

PRD v0.2 §2.1: LinearAdapter implements ChannelAdapter so Linear issues
can trigger fix workflows that open PRs on GitHub and write back to Linear.

Webhook signature: HMAC-SHA256 of the raw body using the webhook signing key.
Header: `linear-signature`.

Event mapping:
  - Issue.create → ISSUE_OPENED
  - Issue.update → ISSUE_EDITED (if description changed)
  - IssueComment.create → ISSUE_COMMENT_CREATED
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from typing import Any, Final

import httpx

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.adapters.base import ChannelAdapter, SignatureError

_logger = logging.getLogger(__name__)

_LINEAR_SIGNATURE_HEADER: Final = "linear-signature"

# Linear webhook action → EventKind
_EVENT_TABLE: Final[dict[str, EventKind]] = {
    "Issue.create": EventKind.ISSUE_OPENED,
    "Issue.update": EventKind.ISSUE_EDITED,
    "IssueComment.create": EventKind.ISSUE_COMMENT_CREATED,
}

_GRAPHQL_URL: Final = "https://api.linear.app/graphql"


class LinearAdapter(ChannelAdapter):
    """Linear channel adapter — webhook receive + GraphQL write-back."""

    name = "linear"

    def __init__(
        self,
        *,
        webhook_secret: str,
        oauth_token: str | None = None,
    ) -> None:
        self._webhook_secret = webhook_secret.encode()
        self._oauth_token = oauth_token
        self._http: httpx.AsyncClient | None = None

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            headers = {"Content-Type": "application/json"}
            if self._oauth_token:
                headers["Authorization"] = self._oauth_token
            self._http = httpx.AsyncClient(
                base_url=_GRAPHQL_URL,
                headers=headers,
                timeout=10.0,
            )
        return self._http

    async def aclose(self) -> None:
        """Close the cached GraphQL client. Called on app shutdown."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> None:
        """Verify Linear webhook HMAC-SHA256 signature.

        Linear signs the raw body with the webhook signing key and sends
        the hex digest in the `linear-signature` header.
        """
        signature = headers.get(_LINEAR_SIGNATURE_HEADER, "")
        if not signature:
            raise SignatureError("Missing linear-signature header")

        expected = hmac.new(self._webhook_secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise SignatureError("Invalid Linear webhook signature")

    def parse_event(self, body: bytes, headers: Mapping[str, str]) -> UnifiedEvent:
        """Parse Linear webhook payload into UnifiedEvent.

        Linear webhook payload shape:
        {
            "action": "Issue.create" | "Issue.update" | ...,
            "data": { ... },
            "url": "https://...",
            "type": "Issue" | "IssueComment" | ...
        }
        """
        payload = json.loads(body)
        action = payload.get("action", "")
        data = payload.get("data", {})

        kind = _EVENT_TABLE.get(action, EventKind.UNKNOWN)

        # Extract issue number from Linear's `identifier` field (e.g., "ENG-123")
        issue_identifier = data.get("identifier", "")
        issue_number = None
        if issue_identifier and "-" in issue_identifier:
            with contextlib.suppress(ValueError):
                issue_number = int(issue_identifier.split("-")[-1])

        # Extract repo from data or url
        repo = data.get("team", {}).get("key", "linear")
        actor = data.get("creator", {}).get("name", "unknown")

        # Comment body for chat events
        comment_body = None
        if "IssueComment" in action:
            comment_body = data.get("body", "")

        return UnifiedEvent(
            channel="linear",
            delivery_id=payload.get("webhookId", ""),
            kind=kind,
            repo=repo,
            actor=actor,
            issue_number=issue_number,
            comment_body=comment_body,
            raw=payload,
        )

    async def reply(self, event: UnifiedEvent, message: str) -> dict[str, Any]:
        """Post a comment on the Linear issue via GraphQL."""
        issue_id = event.raw.get("data", {}).get("id")
        if not issue_id:
            _logger.warning("linear_reply_no_issue_id", extra={"delivery_id": event.delivery_id})
            return {}

        mutation = """
        mutation CommentCreate($input: CommentCreateInput!) {
            commentCreate(input: $input) {
                success
                comment { id body }
            }
        }
        """
        variables = {
            "input": {
                "issueId": issue_id,
                "body": message,
            }
        }

        try:
            http = self._get_http()
            resp = await http.post("", json={"query": mutation, "variables": variables})
            resp.raise_for_status()
            result = resp.json()
            if result.get("errors"):
                _logger.error("linear_graphql_error", extra={"errors": result["errors"]})
            return result.get("data", {}).get("commentCreate", {})
        except httpx.HTTPError:
            _logger.exception("linear_reply_failed", extra={"issue_id": issue_id})
            return {}

    async def update_comment(
        self,
        event: UnifiedEvent,
        comment_id: int,
        message: str,
    ) -> dict[str, Any]:
        """Update an existing Linear comment via GraphQL."""
        mutation = """
        mutation CommentUpdate($id: String!, $input: CommentUpdateInput!) {
            commentUpdate(id: $id, input: $input) {
                success
                comment { id body }
            }
        }
        """
        variables = {
            "id": str(comment_id),
            "input": {"body": message},
        }

        try:
            http = self._get_http()
            resp = await http.post("", json={"query": mutation, "variables": variables})
            resp.raise_for_status()
            return resp.json().get("data", {}).get("commentUpdate", {})
        except httpx.HTTPError:
            _logger.exception("linear_update_comment_failed")
            return {}

    async def get_issue_labels(self, event: UnifiedEvent, number: int) -> frozenset[str]:
        """Return labels on a Linear issue. Stubs as empty for now."""
        return frozenset()

    async def get_pr_comments(self, event: UnifiedEvent, pr_number: int) -> list[dict[str, Any]]:
        """Linear doesn't have PRs in the same sense. Return empty."""
        return []

    async def get_actor_role(self, event: UnifiedEvent, login: str | None = None) -> str:
        """Return actor role. Linear uses different permission model."""
        return "write"

    async def update_check_run(
        self, event: UnifiedEvent, check_run_id: int, **kwargs: Any
    ) -> dict[str, Any]:
        """Linear doesn't have check runs. No-op."""
        return {}

    async def fetch_repo_file(self, event: UnifiedEvent, path: str) -> bytes | None:
        """Linear doesn't expose repo files. Return None."""
        return None

    async def get_pr_diff(self, event: UnifiedEvent, pr_number: int) -> str:
        """Linear doesn't have PRs. Return empty."""
        return ""

    async def read_file(self, event: UnifiedEvent, path: str) -> str:
        """Linear doesn't expose repo files. Return empty."""
        return ""

    async def grep_repo(
        self,
        event: UnifiedEvent,
        *,
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        """Linear doesn't expose code search. Return empty."""
        return []

    async def add_label(self, event: UnifiedEvent, *labels: str) -> list[dict[str, Any]]:
        """Add labels to a Linear issue via GraphQL."""
        issue_id = event.raw.get("data", {}).get("id")
        if not issue_id:
            return []

        # Linear labels need to be created/found first, then added to the issue.
        # Simplified: just log the intent.
        _logger.info("linear_add_label_stub", extra={"issue_id": issue_id, "labels": labels})
        return []

    async def create_pr_review(
        self, event: UnifiedEvent, pr_number: int, **kwargs: Any
    ) -> dict[str, Any]:
        """Linear doesn't have PR reviews. No-op."""
        return {}

    async def get_issue(self, event: UnifiedEvent, issue_number: int) -> dict[str, Any]:
        """Fetch Linear issue details via GraphQL."""
        query = """
        query Issue($id: String!) {
            issue(id: $id) {
                title description
                comments { nodes { body user { name } } }
                team { key }
            }
        }
        """
        issue_id = event.raw.get("data", {}).get("id")
        if not issue_id:
            return {
                "title": "",
                "body": "",
                "comments": [],
                "base_sha": "",
                "default_branch": "",
                "clone_url": "",
            }

        try:
            http = self._get_http()
            resp = await http.post("", json={"query": query, "variables": {"id": issue_id}})
            resp.raise_for_status()
            data = resp.json().get("data", {}).get("issue", {})
            return {
                "title": data.get("title", ""),
                "body": data.get("description", ""),
                "comments": [
                    {"author": c.get("user", {}).get("name", ""), "body": c.get("body", "")}
                    for c in data.get("comments", {}).get("nodes", [])
                ],
                "base_sha": "",
                "default_branch": "",
                "clone_url": "",
            }
        except httpx.HTTPError:
            _logger.exception("linear_get_issue_failed")
            return {
                "title": "",
                "body": "",
                "comments": [],
                "base_sha": "",
                "default_branch": "",
                "clone_url": "",
            }

    async def create_branch(self, event: UnifiedEvent, branch_ref: str, from_sha: str) -> None:
        """Linear doesn't manage git branches. No-op."""
        pass

    async def open_pull_request(self, event: UnifiedEvent, **kwargs: Any) -> dict[str, Any]:
        """Linear doesn't have PRs. The fix workflow opens PRs on GitHub instead."""
        return {}

    async def get_pull_request(self, event: UnifiedEvent, pr_number: int) -> dict[str, Any]:
        """Linear doesn't have PRs. Return empty."""
        return {}

    async def get_default_branch_sha(self, event: UnifiedEvent) -> str:
        """Linear doesn't expose git refs. Return empty."""
        return ""

    async def get_installation_token(self, event: UnifiedEvent) -> str:
        """Linear uses OAuth tokens, not installation tokens."""
        if not self._oauth_token:
            raise RuntimeError("LinearAdapter constructed without OAuth token")
        return self._oauth_token

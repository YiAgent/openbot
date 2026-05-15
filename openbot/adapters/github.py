"""GitHubAdapter — verify HMAC-SHA-256 webhooks + write back via the REST API.

GitHub webhook reference: https://docs.github.com/en/webhooks
GitHub REST reference:    https://docs.github.com/en/rest

PRD §4.8 trust boundary: nothing downstream may touch the payload until
`verify_signature` has returned without raising.

Two responsibilities split inside one class:
  - **Receive**: verify_signature + parse_event  (no auth needed)
  - **Write**:   reply / add_label / remove_label / get_actor_role
                (requires GitHubAppAuth — installation token minting)

Write methods raise `RuntimeError` if the adapter was constructed without
a `GitHubAppAuth`, so a webhook-only deployment never accidentally calls
out to the API.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, Final
from urllib.parse import quote

import httpx

from openbot import __version__
from openbot.adapters.base import ChannelAdapter, SignatureError
from openbot.adapters.github_auth import GitHubAppAuth
from openbot.events import EventKind, UnifiedEvent

_logger = logging.getLogger(__name__)

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


_API_BASE_DEFAULT: Final = "https://api.github.com"
_HTTP_TIMEOUT_SECONDS: Final = 10.0


class GitHubAdapter(ChannelAdapter):
    name = "github"

    def __init__(
        self,
        *,
        webhook_secret: str,
        auth: GitHubAppAuth | None = None,
        http: httpx.AsyncClient | None = None,
        api_base: str = _API_BASE_DEFAULT,
    ) -> None:
        if not webhook_secret:
            raise ValueError("GitHubAdapter requires a non-empty webhook_secret")
        # HMAC needs bytes; store once.
        self._secret = webhook_secret.encode("utf-8")
        self._auth = auth
        self._http = http or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS)
        self._owns_http = http is None
        self._api_base = api_base.rstrip("/")
        self._user_agent = f"OpenBot/{__version__}"

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
        sender = payload.get("sender") or {}
        actor = sender.get("login") or ""
        actor_type = sender.get("type") or None

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
        installation_id = (payload.get("installation") or {}).get("id")

        return UnifiedEvent(
            channel=self.name,
            delivery_id=delivery_id,
            kind=kind,
            repo=repo,
            actor=actor,
            actor_type=actor_type,
            issue_number=issue_number,
            pr_number=pr_number,
            comment_body=comment_body,
            installation_id=installation_id,
            raw=payload,
        )

    # ───────────────────────── write-back API ─────────────────────────

    async def reply(self, event: UnifiedEvent, message: str) -> dict[str, Any]:
        """Post a comment on the issue or PR that triggered `event`.

        Endpoint: POST /repos/{owner}/{repo}/issues/{number}/comments
        (GitHub treats PR comments and issue comments as the same endpoint.)
        """
        number = self._issue_or_pr_number(event)
        url = f"{self._api_base}/repos/{event.repo}/issues/{number}/comments"
        return await self._authed_json("POST", url, event, json_body={"body": message})

    async def add_label(self, event: UnifiedEvent, *labels: str) -> list[dict[str, Any]]:
        """Add one or more labels to the issue/PR. PRD §4.1 triage / §4.2 review."""
        if not labels:
            raise ValueError("add_label requires at least one label")
        number = self._issue_or_pr_number(event)
        url = f"{self._api_base}/repos/{event.repo}/issues/{number}/labels"
        return await self._authed_json("POST", url, event, json_body={"labels": list(labels)})

    async def remove_label(self, event: UnifiedEvent, label: str) -> None:
        """Remove a single label. PRD §4.7: support clearing `cancel-openbot` post-stop."""
        number = self._issue_or_pr_number(event)
        # GitHub returns 200 with the remaining labels, or 404 if not present.
        # `safe=""` so `/` and other path-special chars in label names get encoded.
        url = f"{self._api_base}/repos/{event.repo}/issues/{number}/labels/{quote(label, safe='')}"
        token = await self._installation_token(event)
        response = await self._http.delete(url, headers=self._headers(token.token))
        if response.status_code == 404:
            return  # idempotent: removing an absent label is fine
        response.raise_for_status()

    async def get_actor_role(self, event: UnifiedEvent) -> str:
        """Return the actor's permission level on the repo.

        One of: 'admin' | 'maintain' | 'write' | 'triage' | 'read' | 'none'.
        Drives PRD §4.3 `fix.allowed_actors` and §4.6 `rate_limit.exempt_roles`.
        """
        url = f"{self._api_base}/repos/{event.repo}/collaborators/{event.actor}/permission"
        data = await self._authed_json("GET", url, event)
        return str(data.get("permission") or "none")

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ───────────────────────── internals ─────────────────────────

    async def _authed_json(
        self,
        method: str,
        url: str,
        event: UnifiedEvent,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        token = await self._installation_token(event)
        response = await self._http.request(
            method, url, json=json_body, headers=self._headers(token.token)
        )
        response.raise_for_status()
        # GitHub rate-limit headroom warning — PRD §12 (Anthropic limits) parallel.
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining and remaining.isdigit() and int(remaining) < 100:
            _logger.warning(
                "github_rate_limit_low",
                extra={"remaining": int(remaining), "url": url},
            )
        return response.json() if response.content else None

    async def _installation_token(self, event: UnifiedEvent) -> Any:
        if self._auth is None:
            raise RuntimeError(
                "GitHubAdapter constructed without GitHubAppAuth; write-back unavailable"
            )
        if event.installation_id is None:
            raise ValueError(f"UnifiedEvent has no installation_id (kind={event.kind.value})")
        return await self._auth.installation_token(event.installation_id)

    @staticmethod
    def _issue_or_pr_number(event: UnifiedEvent) -> int:
        number = event.issue_number or event.pr_number
        if number is None:
            raise ValueError(f"event kind={event.kind.value} has no issue/PR number to act on")
        return number

    def _headers(self, token: str) -> dict[str, str]:
        # Token never logged; do not interpolate into log/error messages.
        return {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self._user_agent,
        }

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        # FastAPI gives lowercase keys; be defensive for raw dict callers too.
        return headers.get(name) or headers.get(name.upper()) or headers.get(name.title())

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

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final
from urllib.parse import quote

import httpx
from gidgethub import ValidationFailure
from gidgethub.sansio import validate_event
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from openbot import __version__
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.adapters.base import ChannelAdapter, SignatureError
from openbot.infrastructure.adapters.github_auth import GitHubAppAuth

_logger = logging.getLogger(__name__)

_SIGNATURE_HEADER: Final = "x-hub-signature-256"
_EVENT_HEADER: Final = "x-github-event"
_DELIVERY_HEADER: Final = "x-github-delivery"
_SIGNATURE_PREFIX: Final = "sha256="

# (X-GitHub-Event header, payload.action) → EventKind
# Anything unmapped returns UNKNOWN — we never crash on a new event type.
#
# ``pull_request.closed`` is mapped to PR_CLOSED here, but the parser
# (see ``parse_event``) upgrades it to PR_MERGED when
# ``payload.pull_request.merged is True`` — GitHub overloads the same
# ``closed`` action for both states and only the boolean tells them
# apart.
_EVENT_TABLE: Final[dict[tuple[str, str], EventKind]] = {
    ("issues", "opened"): EventKind.ISSUE_OPENED,
    ("issues", "edited"): EventKind.ISSUE_EDITED,
    ("issues", "assigned"): EventKind.ISSUE_ASSIGNED,
    ("issues", "labeled"): EventKind.ISSUE_LABELED,
    ("issues", "unlabeled"): EventKind.ISSUE_UNLABELED,
    ("issues", "closed"): EventKind.ISSUE_CLOSED,
    ("issues", "reopened"): EventKind.ISSUE_REOPENED,
    ("issue_comment", "created"): EventKind.ISSUE_COMMENT_CREATED,
    ("pull_request", "opened"): EventKind.PR_OPENED,
    ("pull_request", "synchronize"): EventKind.PR_SYNCHRONIZED,
    ("pull_request", "labeled"): EventKind.PR_LABELED,
    ("pull_request", "unlabeled"): EventKind.PR_UNLABELED,
    ("pull_request", "closed"): EventKind.PR_CLOSED,
    ("pull_request", "reopened"): EventKind.PR_REOPENED,
    ("pull_request_review_comment", "created"): EventKind.PR_REVIEW_COMMENT_CREATED,
}


_API_BASE_DEFAULT: Final = "https://api.github.com"
_HTTP_TIMEOUT_SECONDS: Final = 10.0

# Retry policy for GitHub REST calls (PRD §4.8 outbound resilience).
# Three attempts: original + 2 retries. Exponential backoff caps at 4s
# so a 503 storm doesn't push a single webhook past the 45-min agent
# budget (PRD §4.3). LiteLLM owns LLM retry; this only covers
# adapter-level GitHub calls.
_RETRY_MAX_ATTEMPTS: Final = 3
_RETRY_BACKOFF_MIN: Final = 0.5
_RETRY_BACKOFF_MAX: Final = 4.0
# Status codes worth retrying. 502/503/504 are transient gateway errors;
# anything else (401/403/404/422) is a contract failure that retrying
# only delays. 429 is intentionally NOT retried here — GitHub returns
# Retry-After and we surface that as a low-rate-limit warning instead.
_RETRYABLE_STATUSES: Final = frozenset({502, 503, 504})


def _extract_event_seq(pull_request: dict[str, Any], issue: dict[str, Any]) -> int:
    """Epoch-ms of the resource's ``updated_at`` — monotonic ordering hint.

    GitHub stamps every issue/PR mutation with an ISO-8601 ``updated_at``
    (UTC, second precision). We convert that to epoch milliseconds so the
    state machine can use it as ``last_event_seq`` and drop late
    deliveries (close-before-open after a retry, etc.) as ``stale_event``.

    Returns 0 on any failure to parse — the classifier treats 0 as "no
    high-water mark available" and never spuriously drops on that
    sentinel. We prefer "monotonic check disabled for this event" over
    "blow up the receive path on a malformed payload".
    """
    raw = pull_request.get("updated_at") or issue.get("updated_at")
    if not isinstance(raw, str) or not raw:
        return 0
    try:
        # GitHub uses trailing "Z" for UTC; fromisoformat accepts that
        # natively on 3.11+, which is the project floor.
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _is_retryable_github_error(exc: BaseException) -> bool:
    """Retry on transient network + gateway errors only.

    Kept module-level so tests can monkeypatch / inspect it. Tenacity
    calls this for every raised exception; non-retryable ones bubble
    up immediately.
    """
    if isinstance(exc, httpx.ConnectError | httpx.ReadTimeout | httpx.RemoteProtocolError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUSES
    return False


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
        # gidgethub.sansio.validate_event accepts str; we used to store bytes
        # for the manual HMAC. Keep the bytes form so any future code paths
        # using the raw secret directly still get the right encoding.
        self._secret = webhook_secret.encode("utf-8")
        self._auth = auth
        # Lazy-init the httpx client so callers that only need
        # ``verify_signature`` / ``parse_event`` (sync, pure functions) don't
        # leak a socket pool when the adapter is GC'd without ``aclose``.
        # When the caller passes their own client we never own it.
        self._http: httpx.AsyncClient | None = http
        self._owns_http = http is None
        self._api_base = api_base.rstrip("/")
        self._user_agent = f"OpenBot/{__version__}"

    def _get_http(self) -> httpx.AsyncClient:
        """Return the httpx client, lazily creating one if we own it."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS)
        return self._http

    @property
    def _http_client(self) -> httpx.AsyncClient:
        """Helper to ensure we always get a valid client via _get_http()."""
        return self._get_http()

    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> None:
        """HMAC-SHA-256 trust boundary (PRD §4.8).

        Delegates to ``gidgethub.sansio.validate_event`` — same constant-time
        comparison we used to roll by hand, but tracks any future GitHub
        signature-scheme additions (sha1 was deprecated, sha512 may appear)
        without this code changing. We surface failures as the same
        ``SignatureError`` the dispatcher already catches.
        """
        sent = self._header(headers, _SIGNATURE_HEADER)
        # gidgethub's ``validate_event`` raises if signature is None, but
        # the message ("missing signature") differs from the legacy
        # ``"missing or malformed"`` contract some callers may rely on.
        # Pre-check so the SignatureError text stays stable.
        if not sent or not sent.startswith(_SIGNATURE_PREFIX):
            raise SignatureError("missing or malformed X-Hub-Signature-256")
        try:
            validate_event(body, signature=sent, secret=self._secret.decode())
        except ValidationFailure as exc:
            # ValidationFailure subclasses GidgetHubException; we translate
            # to SignatureError so existing ``except SignatureError`` blocks
            # in webapp.py and tests keep working unchanged.
            raise SignatureError("signature mismatch") from exc

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

        # GitHub overloads ``pull_request.closed`` for both genuine closes
        # and merges — only the boolean ``pull_request.merged`` tells them
        # apart. The state machine treats the two the same (both CANCEL),
        # but auditing wants to see the difference, so we keep distinct
        # EventKinds and promote PR_CLOSED → PR_MERGED here.
        if kind is EventKind.PR_CLOSED and bool(pull_request.get("merged")):
            kind = EventKind.PR_MERGED

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
        event_seq = _extract_event_seq(pull_request, issue)

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
            event_seq=event_seq,
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
        response = await self._get_http().delete(url, headers=self._headers(token.token))
        if response.status_code == 404:
            return  # idempotent: removing an absent label is fine
        response.raise_for_status()

    async def get_actor_role(self, event: UnifiedEvent, login: str | None = None) -> str:
        """Return the permission level of `login` (or event.actor) on the repo.

        One of: 'admin' | 'maintain' | 'write' | 'triage' | 'read' | 'none'.
        Drives PRD §4.3 `fix.allowed_actors` and §4.6 `rate_limit.exempt_roles`.
        Returns 'none' on failure.
        """
        target = login if login is not None else event.actor
        url = f"{self._api_base}/repos/{event.repo}/collaborators/{target}/permission"
        try:
            data = await self._authed_json("GET", url, event)
            return str(data.get("permission") or "none") if isinstance(data, dict) else "none"
        except Exception:
            return "none"

    async def get_issue_labels(self, event: UnifiedEvent, number: int) -> frozenset[str]:
        """Return the set of label names on the given issue/PR number.

        Returns an empty frozenset on failure.
        """
        url = f"{self._api_base}/repos/{event.repo}/issues/{number}/labels"
        try:
            data = await self._authed_json("GET", url, event)
            return frozenset(
                str(item["name"])
                for item in (data or [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            )
        except Exception:
            _logger.exception(
                "get_issue_labels_failed",
                extra={"repo": event.repo, "number": number},
            )
            return frozenset()

    async def get_pr_comments(self, event: UnifiedEvent, pr_number: int) -> list[dict[str, Any]]:
        """Return PR comments (up to 100). Returns [] on failure."""
        url = f"{self._api_base}/repos/{event.repo}/issues/{pr_number}/comments?per_page=100"
        try:
            data = await self._authed_json("GET", url, event)
            return list(data) if isinstance(data, list) else []
        except Exception:
            _logger.exception(
                "get_pr_comments_failed",
                extra={"repo": event.repo, "pr_number": pr_number},
            )
            return []

    async def create_check_run(
        self,
        event: UnifiedEvent,
        name: str,
        head_sha: str,
        status: str = "in_progress",
        started_at: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new check run for the PR's head SHA.

        Endpoint: POST /repos/{owner}/{repo}/check-runs
        """
        url = f"{self._api_base}/repos/{event.repo}/check-runs"
        body = {
            "name": name,
            "head_sha": head_sha,
            "status": status,
        }
        if started_at:
            body["started_at"] = started_at
        if output:
            body["output"] = output
        return await self._authed_json("POST", url, event, json_body=body)

    async def update_check_run(
        self,
        event: UnifiedEvent,
        check_run_id: int,
        status: str = "completed",
        conclusion: str | None = None,
        completed_at: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update an existing check run.

        Endpoint: PATCH /repos/{owner}/{repo}/check-runs/{check_run_id}
        """
        url = f"{self._api_base}/repos/{event.repo}/check-runs/{check_run_id}"
        body: dict[str, Any] = {"status": status}
        if conclusion:
            body["conclusion"] = conclusion
        if completed_at:
            body["completed_at"] = completed_at
        if output:
            body["output"] = output
        return await self._authed_json("PATCH", url, event, json_body=body)

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # ───────────────────────── internals ─────────────────────────

    @retry(
        retry=retry_if_exception(_is_retryable_github_error),
        stop=stop_after_attempt(_RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(
            multiplier=_RETRY_BACKOFF_MIN,
            min=_RETRY_BACKOFF_MIN,
            max=_RETRY_BACKOFF_MAX,
        ),
        # Re-raise the original exception (not tenacity's RetryError) so
        # the dispatcher's existing ``except httpx.HTTPStatusError`` paths
        # see what they always saw.
        reraise=True,
    )
    async def _authed_json(
        self,
        method: str,
        url: str,
        event: UnifiedEvent,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        token = await self._installation_token(event)
        response = await self._get_http().request(
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

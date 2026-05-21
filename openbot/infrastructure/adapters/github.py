"""GitHubAdapter — verify HMAC-SHA-256 webhooks + write back via the REST API.

GitHub webhook reference: https://docs.github.com/en/webhooks
GitHub REST reference:    https://docs.github.com/en/rest

PRD §4.8 trust boundary: nothing downstream may touch the payload until
`verify_signature` has returned without raising.

Two responsibilities split inside one class:
  - **Receive**: verify_signature + parse_event  (no auth needed)
  - **Write**:   key methods include reply / add_label / remove_label / get_actor_role
                (all write methods require GitHubAppAuth — installation token minting)

Write methods raise `RuntimeError` if the adapter was constructed without
a `GitHubAppAuth`, so a webhook-only deployment never accidentally calls
out to the API.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Literal
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
            _logger.exception(
                "get_actor_role_failed",
                extra={"repo": event.repo, "login": target},
            )
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

    async def fetch_repo_file(
        self,
        event: UnifiedEvent,
        path: str,
    ) -> bytes | None:
        """Fetch raw file bytes from the repo via the GitHub Contents API.

        Returns None on 404 (file absent). Raises on other HTTP errors.
        The caller is responsible for any decoding (e.g. UTF-8, YAML).
        """
        token = await self._installation_token(event)
        url = f"{self._api_base}/repos/{event.repo}/contents/{path}"
        headers = self._headers(token.token)
        response = await self._get_http().get(url, headers=headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            _logger.warning(
                "fetch_repo_file_response_not_json",
                extra={"repo": event.repo, "path": path, "status": response.status_code},
            )
            raise
        encoded = payload.get("content") or ""
        encoding = payload.get("encoding") or "base64"
        if encoding != "base64":
            _logger.warning(
                "fetch_repo_file_unexpected_encoding",
                extra={"repo": event.repo, "path": path, "encoding": encoding},
            )
            raise ValueError(f"Unexpected encoding from GitHub Contents API: {encoding!r}")
        return base64.b64decode(encoded, validate=False)

    async def get_pr_diff(self, event: UnifiedEvent, pr_number: int) -> str:
        """Fetch the PR's unified diff via the Accept-header content switch.

        Endpoint: GET /repos/{owner}/{repo}/pulls/{pr_number}
        with ``Accept: application/vnd.github.v3.diff`` — GitHub returns the
        raw diff as ``text/plain`` instead of the usual PR JSON. Bypasses
        ``_authed_json`` because that helper expects a JSON body.

        Returns ``""`` on 404 (closed / deleted PRs must not block review).
        Raises on other HTTP errors so transient gateway failures surface.
        """
        token = await self._installation_token(event)
        url = f"{self._api_base}/repos/{event.repo}/pulls/{pr_number}"
        headers = {**self._headers(token.token), "Accept": "application/vnd.github.v3.diff"}
        response = await self._get_http().get(url, headers=headers)
        if response.status_code == 404:
            return ""
        response.raise_for_status()
        return response.text

    async def read_file(self, event: UnifiedEvent, path: str) -> str:
        """Return UTF-8 text of a repo file, or ``""`` if missing / non-text.

        Delegates to ``fetch_repo_file`` for the bytes-level fetch and then
        decodes. Binary files and non-UTF-8 encodings collapse to ``""`` so
        the review agent doesn't have to branch on the failure mode.
        """
        raw = await self.fetch_repo_file(event, path)
        if raw is None:
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            _logger.info(
                "read_file_not_utf8",
                extra={"repo": event.repo, "path": path, "size": len(raw)},
            )
            return ""

    async def grep_repo(
        self,
        event: UnifiedEvent,
        *,
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        """Search the repo via GitHub Code Search and return ``path: fragment`` hits.

        GitHub Code Search is the cheapest grep we can give the agent — one
        HTTP call vs O(files) for tree-walk-and-scan. Limitations the caller
        should know about:

        - Repo must be indexed (fresh repos may 422; we swallow that).
        - ``path_glob`` is GitHub's ``path:`` qualifier — substring match, not
          a real glob. ``src`` matches any path containing ``src``.
        - Pattern is substring + GitHub boolean syntax, not real regex.

        Format: each hit's first text-match fragment is joined with the path
        on a single line. Line numbers are not surfaced because Code Search
        only returns byte offsets; callers wanting exact lines should
        ``read_file`` the path.
        """
        token = await self._installation_token(event)
        # Quote each qualifier value to survive special chars in pattern/path.
        # ``quote(..., safe="")`` aggressively escapes — GitHub accepts that.
        q_parts = [pattern, f"repo:{event.repo}"]
        if path_glob:
            q_parts.append(f"path:{path_glob}")
        q = " ".join(q_parts)
        url = f"{self._api_base}/search/code?q={quote(q, safe='')}"
        headers = {
            **self._headers(token.token),
            "Accept": "application/vnd.github.text-match+json",
        }
        response = await self._get_http().get(url, headers=headers)
        if response.status_code in (404, 422):
            # 422 = malformed / unindexed; treat both as "no matches" so the
            # review still goes through. PR author shouldn't see "search died".
            _logger.info(
                "grep_repo_no_results",
                extra={"repo": event.repo, "status": response.status_code},
            )
            return []
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            matches = item.get("text_matches")
            if not isinstance(path, str) or not isinstance(matches, list) or not matches:
                continue
            first = matches[0]
            fragment = (
                first.get("fragment", "").splitlines()[0]
                if isinstance(first, dict) and isinstance(first.get("fragment"), str)
                else ""
            )
            out.append(f"{path}: {fragment}".strip())
            if len(out) >= max_matches:
                break
        return out

    async def create_pr_review(
        self,
        event: UnifiedEvent,
        pr_number: int,
        *,
        body: str,
        event_type: Literal["APPROVE", "COMMENT"],
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Submit a PR review with optional inline comments (slice B).

        Endpoint: POST /repos/{owner}/{repo}/pulls/{pr_number}/reviews

        ``event_type`` is locked to APPROVE / COMMENT — OpenBot is advisory
        (PRD §13), never block merge with REQUEST_CHANGES.

        Each entry in ``comments`` must shape as ``{path, line, body}``;
        GitHub rejects inline comments without a line number, so callers
        fold repo-wide findings into ``body`` instead.
        """
        url = f"{self._api_base}/repos/{event.repo}/pulls/{pr_number}/reviews"
        payload: dict[str, Any] = {"body": body, "event": event_type}
        if comments:
            payload["comments"] = comments
        return await self._authed_json("POST", url, event, json_body=payload)

    async def get_default_branch_sha(self, event: UnifiedEvent) -> str:
        """Resolve repo's default branch then return its tip SHA.

        Two ``_authed_json`` calls — both inherit the adapter's retry +
        rate-limit-headroom instrumentation, so callers don't need to
        re-wrap them. Raises on hard HTTP errors (the resolver bubbles
        the failure up to the use case).
        """
        base = f"{self._api_base}/repos/{event.repo}"
        repo_meta = await self._authed_json("GET", base, event)
        default_branch = (
            str(repo_meta.get("default_branch") or "main")
            if isinstance(repo_meta, dict)
            else "main"
        )
        ref = await self._authed_json("GET", f"{base}/git/ref/heads/{default_branch}", event)
        return (
            str(ref["object"]["sha"])
            if isinstance(ref, dict) and isinstance(ref.get("object"), dict)
            else ""
        )

    async def get_issue(self, event: UnifiedEvent, issue_number: int) -> dict[str, Any]:
        """Return a normalized issue snapshot for the fix loop.

        Three GitHub calls (issue, comments, repo metadata) + one for the
        branch ref so we know the SHA to fork the fix branch from. Issued
        sequentially through ``_authed_json`` so the existing retry +
        rate-limit-headroom logging fires for each.
        """
        base = f"{self._api_base}/repos/{event.repo}"
        issue = await self._authed_json("GET", f"{base}/issues/{issue_number}", event)
        comments_raw = await self._authed_json(
            "GET", f"{base}/issues/{issue_number}/comments", event
        )
        repo_meta = await self._authed_json("GET", base, event)
        default_branch = (
            str(repo_meta.get("default_branch") or "main")
            if isinstance(repo_meta, dict)
            else "main"
        )
        ref = await self._authed_json("GET", f"{base}/git/ref/heads/{default_branch}", event)
        base_sha = (
            str(ref["object"]["sha"])
            if isinstance(ref, dict) and isinstance(ref.get("object"), dict)
            else ""
        )

        comments: list[dict[str, str]] = []
        if isinstance(comments_raw, list):
            for c in comments_raw:
                if not isinstance(c, dict):
                    continue
                user = c.get("user") if isinstance(c.get("user"), dict) else {}
                comments.append(
                    {
                        "author": str(user.get("login") or "unknown"),
                        "body": str(c.get("body") or ""),
                    }
                )

        return {
            "title": str(issue.get("title") or "") if isinstance(issue, dict) else "",
            "body": str(issue.get("body") or "") if isinstance(issue, dict) else "",
            "comments": comments,
            "base_sha": base_sha,
            "default_branch": default_branch,
            "clone_url": (
                str(repo_meta.get("clone_url") or f"https://github.com/{event.repo}.git")
                if isinstance(repo_meta, dict)
                else f"https://github.com/{event.repo}.git"
            ),
        }

    async def create_branch(
        self,
        event: UnifiedEvent,
        branch_ref: str,
        from_sha: str,
    ) -> None:
        """POST /repos/{r}/git/refs — prepend ``refs/heads/`` (GitHub requires it)."""
        url = f"{self._api_base}/repos/{event.repo}/git/refs"
        await self._authed_json(
            "POST",
            url,
            event,
            json_body={"ref": f"refs/heads/{branch_ref}", "sha": from_sha},
        )

    async def open_pull_request(
        self,
        event: UnifiedEvent,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, Any]:
        """POST /repos/{r}/pulls — never sends ``draft`` (default False)."""
        url = f"{self._api_base}/repos/{event.repo}/pulls"
        return await self._authed_json(
            "POST",
            url,
            event,
            json_body={"title": title, "body": body, "head": head, "base": base},
        )

    async def get_installation_token(self, event: UnifiedEvent) -> str:
        """Expose the raw bearer for sandbox push URLs (see port docstring)."""
        token = await self._installation_token(event)
        return str(token.token)

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

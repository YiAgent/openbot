"""ChannelAdapterPort — channel-agnostic interaction surface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from openbot.domain.events import UnifiedEvent


@runtime_checkable
class ChannelAdapterPort(Protocol):
    """One per channel. Currently only GitHub is implemented."""

    name: str

    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> None:
        """Raise SignatureError on auth failure."""
        ...

    def parse_event(self, body: bytes, headers: Mapping[str, str]) -> UnifiedEvent:
        """Decode the authenticated payload."""
        ...

    async def reply(self, event: UnifiedEvent, message: str) -> dict[str, Any]:
        """Post a reply on the originating thread."""
        ...

    async def get_issue_labels(self, event: UnifiedEvent, number: int) -> frozenset[str]:
        """Return the set of label names on the given issue/PR number.

        Returns an empty frozenset on failure.
        """
        ...

    async def get_pr_comments(self, event: UnifiedEvent, pr_number: int) -> list[dict[str, Any]]:
        """Return PR comments (up to 100). Returns [] on failure."""
        ...

    async def get_actor_role(self, event: UnifiedEvent, login: str | None = None) -> str:
        """Return the repo permission role of `login` (or event.actor if None).

        One of: 'admin' | 'maintain' | 'write' | 'triage' | 'read' | 'none'.
        Returns 'none' on failure.
        """
        ...

    async def update_check_run(
        self,
        event: UnifiedEvent,
        check_run_id: int,
        status: str = "completed",
        conclusion: str | None = None,
        completed_at: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update an existing check run's status and output.

        Args:
            event: The unified event context.
            check_run_id: The numeric ID of the check run to update.
            status: The new status (e.g., 'completed').
            conclusion: The conclusion (e.g., 'success', 'failure', 'skipped').
            completed_at: Timestamp when the check run completed.
            output: Output dict with 'title' and 'summary' fields.

        Returns:
            The updated check run object.
        """
        ...

    async def fetch_repo_file(
        self,
        event: UnifiedEvent,
        path: str,
    ) -> bytes | None:
        """Fetch a file from the repo at `path`. Returns None if not found (404).

        Raises on other HTTP errors (auth failures, server errors).
        """
        ...

    async def get_pr_diff(self, event: UnifiedEvent, pr_number: int) -> str:
        """Return the PR's unified diff as raw text.

        Returns ``""`` when the PR is missing (404) — closed or deleted PRs
        must not block the review workflow. Raises on other HTTP errors so
        the caller's retry layer can act.
        """
        ...

    async def read_file(self, event: UnifiedEvent, path: str) -> str:
        """Return the UTF-8 text of a repo file at ``path``.

        Returns ``""`` when the file is missing (404) or not UTF-8 decodable —
        callers (review tools) treat an empty string as "no readable content
        here" and do not branch on the failure mode. Implementations should
        log the discriminating detail (404 vs decode error vs other).
        """
        ...

    async def grep_repo(
        self,
        event: UnifiedEvent,
        *,
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        """Search the repo for ``pattern`` and return up to ``max_matches`` hits.

        Each hit is a single line formatted ``"{path}: {fragment}"`` — line
        numbers are not promised because the underlying backend (GitHub Code
        Search) only returns byte fragments. ``path_glob`` filters by GitHub
        Code Search's ``path:`` qualifier (substring, not real glob).

        Returns ``[]`` when nothing matches or the backend rejects the query
        (e.g. 422 on unindexed repos) — callers must not infer "absent" from
        an empty list; they should fall back to ``read_file`` on a known path.
        """
        ...

    async def add_label(self, event: UnifiedEvent, *labels: str) -> list[dict[str, Any]]:
        """Add one or more labels to the issue or PR referenced by *event*.

        Returns a list of created label objects (same shape as GitHub API response).
        Implementations may return [] on a no-op or if labels already exist.
        """
        ...

    async def create_pr_review(
        self,
        event: UnifiedEvent,
        pr_number: int,
        *,
        body: str,
        event_type: Literal["APPROVE", "COMMENT"],
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Submit a single PR review (body + inline comments + verdict).

        ``event_type`` is restricted to ``APPROVE`` and ``COMMENT`` on purpose:
        OpenBot is advisory in v0.1 and must never block a merge with
        ``REQUEST_CHANGES`` (PRD §13 — reviewer-locked decision).

        ``comments`` is a list of ``{path, line, body}`` dicts (GitHub's PR
        Review API shape). Repo-wide findings without a line number belong
        in ``body``, not in this list — GitHub requires a line for every
        inline comment.

        Returns the GitHub PR review object on success. Raises on HTTP error
        — the caller treats a failed review submission as a workflow failure.
        """
        ...

    async def get_issue(
        self,
        event: UnifiedEvent,
        issue_number: int,
    ) -> dict[str, Any]:
        """Return a normalized snapshot of an issue.

        Shape (all keys always present):
            {
              "title": str,
              "body": str,                # "" if GitHub returned null
              "comments": list[dict],     # [{"author": str, "body": str}, ...]
              "base_sha": str,            # default branch HEAD
              "default_branch": str,
              "clone_url": str,
            }

        Implementations batch the underlying calls (issue + comments +
        repo metadata + branch ref). Raises ``httpx.HTTPStatusError`` on
        any non-2xx (the use case catches the 404 case to skip silently).
        """
        ...

    async def create_branch(
        self,
        event: UnifiedEvent,
        branch_ref: str,
        from_sha: str,
    ) -> None:
        """Create a new branch ``branch_ref`` pointing at ``from_sha``.

        ``branch_ref`` is the short ref (e.g. ``openbot/fix-issue-42-deadbee``);
        implementations prepend ``refs/heads/`` for GitHub's git-refs endpoint.

        Raises ``httpx.HTTPStatusError(422)`` if the branch already exists —
        the use case surfaces that as "open fix attempt already pending".
        """
        ...

    async def open_pull_request(
        self,
        event: UnifiedEvent,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, Any]:
        """Open a non-draft pull request from ``head`` into ``base``.

        Returns the GitHub PR object (``number``, ``html_url``, ``head``, ...).
        Always non-draft (PRD §13 #2 — fix loop never opens speculative PRs).
        Raises on HTTP error so the use case can post a tailored comment.
        """
        ...

    async def get_installation_token(self, event: UnifiedEvent) -> str:
        """Return a short-lived push token for the event's installation.

        The use case interpolates this into the clone/push URL:
        ``https://x-access-token:{token}@github.com/{repo}.git``. Token is
        opaque, single-use from the use case's perspective; adapter caches
        and refreshes it internally.

        Raises if the adapter was constructed without auth (e.g., webhook-
        only mode without a GitHub App).
        """
        ...

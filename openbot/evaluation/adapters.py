"""EvalChannelAdapter — offline ChannelAdapterPort for evals.

Captures output (replies, reviews) instead of posting to GitHub.
Raises EvalSideEffectError on operations that would mutate GitHub state
(create_branch, open_pull_request) so eval runs are safely isolated.

Design notes:
  - Fields follow the same pattern as FakeChannelAdapter in tests/_fakes/
    but are eval-specific (no token/SHA lookups needed).
  - ``pr_diff`` is the raw diff text the review responder reads via
    ``get_pr_diff``; set it to the sample's diff before calling the runner.
  - ``files`` maps repo-relative paths → file contents, used by
    ``read_file`` and ``grep_repo``.  Acts as a priority cache: explicit
    entries in ``files`` always win over the ``file_reader`` fallback.
  - ``file_reader`` is an optional ``GitHubFileReader`` injected by the
    solver.  When set, ``read_file`` delegates cache misses to it and
    ``grep_repo`` delegates entirely when ``files`` is empty.  This keeps
    file-fetching inside OpenBot's GitHub infrastructure layer instead of
    duplicating it in Inspect AI eval code.
  - ``issue`` is the dict shape returned by ``get_issue``; the fix
    responder reads ``title``, ``body``, and ``base_sha``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from openbot.domain.events import EventKind, UnifiedEvent

if TYPE_CHECKING:
    from openbot.evaluation.github_file_reader import GitHubFileReader


class EvalSideEffectError(RuntimeError):
    """Raised when eval code invokes a GitHub write operation.

    All production write-back paths (create_branch, open_pull_request)
    raise this so an accidental side-effect in an eval run is loud
    rather than silently posting to GitHub.
    """


@dataclass
class EvalChannelAdapter:
    """Offline ChannelAdapterPort that records output and blocks side-effects.

    Construct once per sample; share across the adapter calls made during
    a single eval inference run.
    """

    # The raw diff text for the PR being evaluated.
    pr_diff: str
    # Optional repo file map for read_file / grep_repo.
    # Acts as a priority cache: entries here win over file_reader.
    files: dict[str, str] = field(default_factory=dict)
    # Optional GitHub file reader injected by the solver.  When set, read_file
    # falls back to it on cache misses and grep_repo delegates entirely when
    # files is empty — keeping GitHub fetching inside OpenBot's infra layer.
    file_reader: GitHubFileReader | None = field(default=None)
    # Optional issue dict for get_issue.
    issue: dict[str, Any] = field(
        default_factory=lambda: {
            "title": "",
            "body": "",
            "comments": [],
            "base_sha": "0" * 40,
            "default_branch": "main",
            "clone_url": "https://github.com/example/repo.git",
        }
    )

    # Recording fields — read by eval scorers after the run.
    replies: list[tuple[str | None, str]] = field(default_factory=list)
    pr_reviews: list[dict[str, Any]] = field(default_factory=list)
    labels_added: list[tuple[str | None, tuple[str, ...]]] = field(default_factory=list)

    # ── ChannelAdapterPort boilerplate ──────────────────────────────────────

    name: str = "eval"

    def verify_signature(self, body: bytes, headers: Any) -> None:
        return  # always accept in eval context

    def parse_event(self, body: bytes, headers: Any) -> UnifiedEvent:
        return UnifiedEvent(
            channel=self.name,
            delivery_id="",
            kind=EventKind.UNKNOWN,
            repo="",
            actor="",
        )

    # ── reads ────────────────────────────────────────────────────────────────

    async def get_issue_labels(self, event: UnifiedEvent, number: int) -> frozenset[str]:
        return frozenset()

    async def get_pr_comments(self, event: UnifiedEvent, pr_number: int) -> list[dict[str, Any]]:
        return []

    async def get_actor_role(self, event: UnifiedEvent, login: str | None = None) -> str:
        return "none"

    async def fetch_repo_file(self, event: UnifiedEvent, path: str) -> bytes | None:
        return None

    async def get_pr_diff(self, event: UnifiedEvent, pr_number: int) -> str:
        return self.pr_diff

    async def read_file(self, event: UnifiedEvent, path: str) -> str:
        """Return file content, preferring the ``files`` cache then ``file_reader``."""
        if path in self.files:
            return self.files[path]
        if self.file_reader is not None:
            return await self.file_reader.read_file(path)
        return ""

    async def grep_repo(
        self,
        event: UnifiedEvent,
        *,
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        """Search for ``pattern``.

        When ``files`` is populated (unit-test fixtures or explicit overrides)
        the in-memory regex scan runs.  Otherwise delegates to ``file_reader``
        which uses GitHub Code Search — keeping file-fetching inside OpenBot's
        GitHub infrastructure layer.
        """
        if self.files:
            results: list[str] = []
            try:
                rx = re.compile(pattern)
            except re.error:
                return []
            for path, content in self.files.items():
                for lineno, line in enumerate(content.splitlines(), start=1):
                    if rx.search(line):
                        results.append(f"{path}:{lineno}:{line}")
                        if len(results) >= max_matches:
                            return results
            return results
        if self.file_reader is not None:
            return await self.file_reader.grep_repo(
                pattern=pattern,
                path_glob=path_glob,
                max_matches=max_matches,
            )
        return []

    async def get_issue(self, event: UnifiedEvent, issue_number: int) -> dict[str, Any]:
        return dict(self.issue)

    async def get_pull_request(self, event: UnifiedEvent, pr_number: int) -> dict[str, Any]:
        return {
            "head": {"sha": "a" * 40, "ref": "feature"},
            "base": {"sha": "b" * 40, "ref": "main"},
            "number": pr_number,
            "state": "open",
        }

    async def get_default_branch_sha(self, event: UnifiedEvent) -> str:
        return "0" * 40

    async def get_installation_token(self, event: UnifiedEvent) -> str:
        return "fake-eval-token"

    async def update_check_run(
        self, event: UnifiedEvent, check_run_id: int, **kwargs: Any
    ) -> dict[str, Any]:
        return {"ok": True, "id": check_run_id}

    # ── writes (captured) ────────────────────────────────────────────────────

    async def reply(self, event: UnifiedEvent, message: str) -> dict[str, Any]:
        self.replies.append((event.resource_key, message))
        return {"ok": True, "id": len(self.replies)}

    async def add_label(self, event: UnifiedEvent, *labels: str) -> list[dict[str, Any]]:
        self.labels_added.append((event.resource_key, labels))
        return [{"name": lbl} for lbl in labels]

    async def create_pr_review(
        self,
        event: UnifiedEvent,
        pr_number: int,
        *,
        body: str,
        event_type: Literal["APPROVE", "COMMENT"],
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        record = {
            "resource_key": event.resource_key,
            "pr_number": pr_number,
            "body": body,
            "event_type": event_type,
            "comments": list(comments or ()),
        }
        self.pr_reviews.append(record)
        return {"ok": True, "id": len(self.pr_reviews), **record}

    # ── blocked side-effects ─────────────────────────────────────────────────

    async def create_branch(self, event: UnifiedEvent, branch_ref: str, from_sha: str) -> None:
        raise EvalSideEffectError(
            f"create_branch called in eval context (branch={branch_ref!r}). "
            "EvalChannelAdapter does not support GitHub write operations."
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
        raise EvalSideEffectError(
            f"open_pull_request called in eval context (head={head!r}). "
            "EvalChannelAdapter does not support GitHub write operations."
        )


__all__ = ["EvalChannelAdapter", "EvalSideEffectError"]

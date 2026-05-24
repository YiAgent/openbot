"""FakeChannelAdapter — accepts every signature, records replies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from openbot.domain.events import EventKind, UnifiedEvent


@dataclass
class FakeChannelAdapter:
    name: str = "fake"
    parsed_event: UnifiedEvent | None = None
    replies: list[tuple[str | None, str]] = field(default_factory=list)
    labels_added: list[tuple[str | None, tuple[str, ...]]] = field(default_factory=list)
    pr_reviews: list[dict[str, Any]] = field(default_factory=list)
    # ── Slice C fix-loop recording fields ──
    issue_lookups: list[tuple[str | None, int]] = field(default_factory=list)
    branch_creates: list[tuple[str | None, str, str]] = field(default_factory=list)
    pr_creates: list[dict[str, Any]] = field(default_factory=list)
    token_lookups: list[str | None] = field(default_factory=list)
    fake_issue: dict[str, Any] = field(
        default_factory=lambda: {
            "title": "stub issue",
            "body": "stub body",
            "comments": [],
            "base_sha": "0" * 40,
            "default_branch": "main",
            "clone_url": "https://github.com/example/repo.git",
        }
    )
    fake_pr: dict[str, Any] = field(
        default_factory=lambda: {
            "number": 1,
            "html_url": "https://github.com/example/repo/pull/1",
        }
    )
    fake_installation_token: str = "fake-install-token"
    # Constant SHA returned by ``get_default_branch_sha`` — exposed as a
    # field so the few resolver tests that care can override it without
    # subclassing.
    fake_default_branch_sha: str = "0" * 40
    default_branch_sha_lookups: list[str | None] = field(default_factory=list)
    # Constant PR payload returned by ``get_pull_request`` — exposed as
    # a field so resolver tests can override head/base SHAs without
    # subclassing. Only ``head.sha`` / ``base.sha`` are read by callers
    # in v0.1; other keys are present for shape parity with GitHub.
    fake_pull_request: dict[str, Any] = field(
        default_factory=lambda: {
            "head": {"sha": "a" * 40, "ref": "feature"},
            "base": {"sha": "b" * 40, "ref": "main"},
            "number": 1,
            "state": "open",
        }
    )
    pull_request_lookups: list[tuple[str | None, int]] = field(default_factory=list)
    # Records every ``update_check_run`` call so dispatcher / worker tests
    # can assert on conclusion + summary without subclassing.
    check_run_updates: list[dict[str, Any]] = field(default_factory=list)

    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> None:
        return  # always accept

    def parse_event(self, body: bytes, headers: Mapping[str, str]) -> UnifiedEvent:
        if self.parsed_event is None:
            return UnifiedEvent(
                channel=self.name,
                delivery_id="",
                kind=EventKind.UNKNOWN,
                repo="",
                actor="",
            )
        return self.parsed_event

    async def reply(self, event: UnifiedEvent, message: str) -> dict[str, Any]:
        self.replies.append((event.resource_key, message))
        return {"ok": True, "id": len(self.replies)}

    async def get_issue_labels(self, event: UnifiedEvent, number: int) -> frozenset[str]:
        return frozenset()

    async def get_pr_comments(self, event: UnifiedEvent, pr_number: int) -> list[dict[str, Any]]:
        return []

    async def get_actor_role(self, event: UnifiedEvent, login: str | None = None) -> str:
        return "none"

    async def update_check_run(
        self,
        event: UnifiedEvent,
        check_run_id: int,
        status: str = "completed",
        conclusion: str | None = None,
        completed_at: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.check_run_updates.append(
            {
                "resource_key": event.resource_key,
                "check_run_id": check_run_id,
                "status": status,
                "conclusion": conclusion,
                "completed_at": completed_at,
                "output": dict(output) if output else None,
            }
        )
        return {"ok": True, "id": check_run_id}

    async def fetch_repo_file(self, event: UnifiedEvent, path: str) -> bytes | None:
        return None  # no config file by default in tests

    async def get_pr_diff(self, event: UnifiedEvent, pr_number: int) -> str:
        return ""  # empty diff = "no changes to review" in tests

    async def read_file(self, event: UnifiedEvent, path: str) -> str:
        return ""  # empty text = "file not interesting" — tools handle gracefully

    async def grep_repo(
        self,
        event: UnifiedEvent,
        *,
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        return []  # empty = "no matches"; callers must not infer absence

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

    async def get_issue(self, event: UnifiedEvent, issue_number: int) -> dict[str, Any]:
        self.issue_lookups.append((event.resource_key, issue_number))
        # Return a *copy* so test mutations don't poison the shared default.
        return dict(self.fake_issue)

    async def create_branch(self, event: UnifiedEvent, branch_ref: str, from_sha: str) -> None:
        self.branch_creates.append((event.resource_key, branch_ref, from_sha))

    async def open_pull_request(
        self,
        event: UnifiedEvent,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, Any]:
        record = {
            "resource_key": event.resource_key,
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        }
        self.pr_creates.append(record)
        return dict(self.fake_pr)

    async def get_installation_token(self, event: UnifiedEvent) -> str:
        self.token_lookups.append(event.resource_key)
        return self.fake_installation_token

    async def get_default_branch_sha(self, event: UnifiedEvent) -> str:
        self.default_branch_sha_lookups.append(event.resource_key)
        return self.fake_default_branch_sha

    async def get_pull_request(self, event: UnifiedEvent, pr_number: int) -> dict[str, Any]:
        self.pull_request_lookups.append((event.resource_key, pr_number))
        # Return a *copy* so test mutations don't poison the shared default.
        return dict(self.fake_pull_request)

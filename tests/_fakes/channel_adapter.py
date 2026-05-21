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

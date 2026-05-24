"""FakeChannelAdapter — in-memory ChannelAdapterPort.

Mirrors every method in the 19-method ChannelAdapterPort protocol.

Read methods return canned values (configurable via constructor fields).
Write methods record calls as immutable frozen-dataclass tuples:
  ``replies``           — reply() calls
  ``labels_added``      — add_label() calls
  ``check_run_updates`` — update_check_run() calls
  ``posted_reviews``    — create_pr_review() calls
  ``branch_creates``    — create_branch() calls
  ``pr_opens``          — open_pull_request() calls

``verify_signature`` is a no-op by default; set ``raise_on_verify=True``
to make it raise ``ValueError`` (simulating HMAC mismatch).

``parse_event`` returns ``fake_event``; callers must configure it before
calling parse_event. The ``_PROTOCOL_CHECK`` sentinel never calls it.

Failure injection: ``fail_after=N`` / ``fail_with=…`` applies to ALL
write calls (reply, add_label, update_check_run, create_pr_review,
create_branch, open_pull_request) counted together.

All-defaults constructor: ``FakeChannelAdapter()`` works with no args.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

from openbot.application.ports.channel_adapter import ChannelAdapterPort

if TYPE_CHECKING:
    from collections.abc import Mapping

    from openbot.domain.events import UnifiedEvent


# ── Observation records ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class BranchRecord:
    """Snapshot of one create_branch() call."""

    repo: str | None
    branch_ref: str
    from_sha: str


@dataclass(frozen=True)
class CheckRunRecord:
    """Snapshot of one update_check_run() call."""

    repo: str | None
    check_run_id: int
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class LabelRecord:
    """Snapshot of one add_label() call."""

    repo: str | None
    labels: tuple[str, ...]


@dataclass(frozen=True)
class PROpenRecord:
    """Snapshot of one open_pull_request() call."""

    repo: str | None
    title: str
    head: str
    base: str


@dataclass(frozen=True)
class ReplyRecord:
    """Snapshot of one reply() call."""

    repo: str | None
    message: str


@dataclass(frozen=True)
class ReviewRecord:
    """Snapshot of one create_pr_review() call."""

    repo: str | None
    pr_number: int
    body: str
    event_type: str


# ── Fake implementation ───────────────────────────────────────────────────────


@dataclass
class FakeChannelAdapter:
    """In-memory ChannelAdapterPort. Construct fresh per test."""

    name: str = "fake"
    raise_on_verify: bool = False
    fake_event: UnifiedEvent | None = None
    fake_labels: frozenset[str] = field(default_factory=frozenset)
    fake_pr_comments: list[dict[str, Any]] = field(default_factory=list)
    fake_actor_role: str = "write"
    fake_file: bytes | None = None
    fake_pr_diff: str = ""
    fake_file_text: str = ""
    fake_grep_results: list[str] = field(default_factory=list)
    fake_issue: dict[str, Any] = field(
        default_factory=lambda: {
            "title": "stub",
            "body": "stub body",
            "comments": [],
            "base_sha": "0" * 40,
            "default_branch": "main",
            "clone_url": "https://github.com/example/repo.git",
        }
    )
    fake_pull_request: dict[str, Any] = field(
        default_factory=lambda: {
            "head": {"sha": "a" * 40, "ref": "feature"},
            "base": {"sha": "b" * 40, "ref": "main"},
            "number": 1,
            "state": "open",
        }
    )
    fake_default_branch_sha: str = "0" * 40
    fake_installation_token: str = "fake-token"
    fake_check_run: dict[str, Any] = field(default_factory=lambda: {"id": 1})
    fake_pr: dict[str, Any] = field(
        default_factory=lambda: {
            "number": 1,
            "html_url": "https://github.com/example/repo/pull/1",
        }
    )
    fail_after: int | None = None
    fail_with: type[Exception] = RuntimeError

    _replies: list[ReplyRecord] = field(default_factory=list, init=False)
    _labels_added: list[LabelRecord] = field(default_factory=list, init=False)
    _check_run_updates: list[CheckRunRecord] = field(default_factory=list, init=False)
    _posted_reviews: list[ReviewRecord] = field(default_factory=list, init=False)
    _branch_creates: list[BranchRecord] = field(default_factory=list, init=False)
    _pr_opens: list[PROpenRecord] = field(default_factory=list, init=False)
    _write_count: int = field(default=0, init=False)

    # ── Observation properties ─────────────────────────────────────────────

    @property
    def replies(self) -> tuple[ReplyRecord, ...]:
        return tuple(self._replies)

    @property
    def labels_added(self) -> tuple[LabelRecord, ...]:
        return tuple(self._labels_added)

    @property
    def check_run_updates(self) -> tuple[CheckRunRecord, ...]:
        return tuple(self._check_run_updates)

    @property
    def posted_reviews(self) -> tuple[ReviewRecord, ...]:
        return tuple(self._posted_reviews)

    @property
    def branch_creates(self) -> tuple[BranchRecord, ...]:
        return tuple(self._branch_creates)

    @property
    def pr_opens(self) -> tuple[PROpenRecord, ...]:
        return tuple(self._pr_opens)

    # ── Sync methods ────────────────────────────────────────────────────────

    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> None:
        """No-op unless ``raise_on_verify=True``."""
        if self.raise_on_verify:
            raise ValueError("FakeChannelAdapter: simulated signature failure")

    def parse_event(self, body: bytes, headers: Mapping[str, str]) -> UnifiedEvent:
        """Return ``fake_event``; configure it before calling this method."""
        return self.fake_event  # type: ignore[return-value]

    # ── Read-only async methods ─────────────────────────────────────────────

    async def get_issue_labels(self, event: UnifiedEvent, number: int) -> frozenset[str]:
        return self.fake_labels

    async def get_pr_comments(self, event: UnifiedEvent, pr_number: int) -> list[dict[str, Any]]:
        return self.fake_pr_comments

    async def get_actor_role(self, event: UnifiedEvent, login: str | None = None) -> str:
        return self.fake_actor_role

    async def fetch_repo_file(self, event: UnifiedEvent, path: str) -> bytes | None:
        return self.fake_file

    async def get_pr_diff(self, event: UnifiedEvent, pr_number: int) -> str:
        return self.fake_pr_diff

    async def read_file(self, event: UnifiedEvent, path: str) -> str:
        return self.fake_file_text

    async def grep_repo(
        self,
        event: UnifiedEvent,
        *,
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        return self.fake_grep_results

    async def get_issue(self, event: UnifiedEvent, issue_number: int) -> dict[str, Any]:
        return self.fake_issue

    async def get_pull_request(self, event: UnifiedEvent, pr_number: int) -> dict[str, Any]:
        return self.fake_pull_request

    async def get_default_branch_sha(self, event: UnifiedEvent) -> str:
        return self.fake_default_branch_sha

    async def get_installation_token(self, event: UnifiedEvent) -> str:
        return self.fake_installation_token

    # ── Write async methods (recorded + failure-injectable) ─────────────────

    async def reply(self, event: UnifiedEvent, message: str) -> dict[str, Any]:
        """See :class:`openbot.application.ports.channel_adapter.ChannelAdapterPort`."""
        self._maybe_fail()
        self._replies.append(ReplyRecord(repo=event.repo, message=message))
        return {}

    async def add_label(self, event: UnifiedEvent, *labels: str) -> list[dict[str, Any]]:
        """See :class:`openbot.application.ports.channel_adapter.ChannelAdapterPort`."""
        self._maybe_fail()
        self._labels_added.append(LabelRecord(repo=event.repo, labels=tuple(labels)))
        return []

    async def update_check_run(
        self,
        event: UnifiedEvent,
        check_run_id: int,
        status: str = "completed",
        conclusion: str | None = None,
        completed_at: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """See :class:`openbot.application.ports.channel_adapter.ChannelAdapterPort`."""
        self._maybe_fail()
        self._check_run_updates.append(
            CheckRunRecord(
                repo=event.repo,
                check_run_id=check_run_id,
                status=status,
                conclusion=conclusion,
            )
        )
        return self.fake_check_run

    async def create_pr_review(
        self,
        event: UnifiedEvent,
        pr_number: int,
        *,
        body: str,
        event_type: Literal["APPROVE", "COMMENT"],
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """See :class:`openbot.application.ports.channel_adapter.ChannelAdapterPort`."""
        self._maybe_fail()
        self._posted_reviews.append(
            ReviewRecord(repo=event.repo, pr_number=pr_number, body=body, event_type=event_type)
        )
        return {}

    async def create_branch(self, event: UnifiedEvent, branch_ref: str, from_sha: str) -> None:
        """See :class:`openbot.application.ports.channel_adapter.ChannelAdapterPort`."""
        self._maybe_fail()
        self._branch_creates.append(
            BranchRecord(repo=event.repo, branch_ref=branch_ref, from_sha=from_sha)
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
        """See :class:`openbot.application.ports.channel_adapter.ChannelAdapterPort`."""
        self._maybe_fail()
        self._pr_opens.append(PROpenRecord(repo=event.repo, title=title, head=head, base=base))
        return self.fake_pr

    def _maybe_fail(self) -> None:
        # Failure budget is shared across all write methods.
        self._write_count += 1
        if self.fail_after is not None and self._write_count > self.fail_after:
            raise self.fail_with("FakeChannelAdapter: simulated failure")


# Static check: import-time error if FakeChannelAdapter stops satisfying ChannelAdapterPort.
_PROTOCOL_CHECK: Final[ChannelAdapterPort] = FakeChannelAdapter()


__all__ = [
    "BranchRecord",
    "CheckRunRecord",
    "FakeChannelAdapter",
    "LabelRecord",
    "PROpenRecord",
    "ReplyRecord",
    "ReviewRecord",
]

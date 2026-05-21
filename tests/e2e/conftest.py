"""E2E harness — composes adapter + redis + sqlite for one webhook flow.

Design notes
============

The fixture builds a **WebhookHarness** with three swappable backends:

  - ``adapter``         a ``RecordingGitHubAdapter`` that subclasses the real
                        ``GitHubAdapter`` so signature verification and the
                        ``_authed_json`` plumbing still work bit-for-bit, but
                        every write-back call (``reply`` / ``add_label`` /
                        ``remove_label`` / ``get_actor_role``) is recorded
                        instead of sent. ``_authed_json`` reads are answered
                        from caller-controlled tables on the adapter.
  - ``redis``           ``fakeredis.aioredis.FakeRedis``.
  - ``session_factory`` aiosqlite in-memory engine + sessionmaker; schema
                        created once per fixture.

We deliberately bypass ``TestClient(app)`` for chain-behavior tests because
the webhook → queue serialization is already locked down by
``tests/test_webhook_endpoint.py``. Demos 1-8 call ``run_dispatch`` directly
with a synthetic ``UnifiedEvent``; demo 9 exercises the worker loop end-to-
end. Doing it this way keeps each test under 60 lines and avoids tying
demo assertions to the specific shape of FastAPI BackgroundTask scheduling.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import fakeredis.aioredis
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from openbot.application.dispatcher import run_dispatch
from openbot.application.router import dispatch_for
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.adapters.github import GitHubAdapter
from openbot.infrastructure.config_loader import EffectiveConfig, baked_in_defaults
from openbot.infrastructure.persistence.models import AuditLog, Base
from openbot.infrastructure.persistence.rate_limiter_redis import RedisRateLimiter

if TYPE_CHECKING:
    from openbot.application.router import Dispatch

# Test-only webhook secret — only used inside RecordingGitHubAdapter so
# the parent class' constructor doesn't reject an empty string.
_E2E_SECRET = "e2e-test-secret"


class RecordingGitHubAdapter(GitHubAdapter):
    """``GitHubAdapter`` with write-back replaced by in-memory recording.

    We subclass the real adapter (not just AsyncMock) so:

      - ``verify_signature`` / ``parse_event`` still work — useful when
        a future test wants to round-trip a signed body.
      - ``_authed_json`` and ``_installation_token`` are still the
        injection points downstream middlewares (CancelLabel,
        ForkPRGate) use — we override only those two so the cache
        and reuse pattern is preserved.
      - The adapter's existing `_api_base` / `_user_agent` plumbing
        is exercised so url-construction bugs surface here, not in
        production.
    """

    def __init__(self) -> None:
        # ``auth=None`` is fine — we override the only path that needs it.
        super().__init__(webhook_secret=_E2E_SECRET, auth=None)
        # Recorded write-backs — tests assert against these.
        self.replies: list[tuple[str, int | None, str]] = []  # (repo, number, body)
        self.labels_added: list[tuple[str, tuple[str, ...]]] = []
        self.labels_removed: list[tuple[str, str]] = []
        # Inputs the test controls: actor → role, label list to return,
        # comment list to return on issue/PR comments fetch.
        self.actor_roles: dict[str, str] = {}
        self.labels_response: list[dict[str, Any]] = []
        self.comments_response: list[dict[str, Any]] = []
        # Slice-A2 review tools — test-controlled file content + grep hits.
        # ``file_responses[path]`` overrides the default empty string.
        # ``grep_responses[(pattern, path_glob)]`` returns the canned list.
        self.file_responses: dict[str, str] = {}
        self.grep_responses: dict[tuple[str, str | None], list[str]] = {}

    async def reply(self, event: UnifiedEvent, message: str) -> dict[str, Any]:
        """Record + return a synthetic comment id (matches real shape)."""
        number = event.issue_number or event.pr_number
        self.replies.append((event.repo, number, message))
        # GitHub's POST /comments returns `{ "id": <int>, ... }`. Workflows
        # only read `id`, so we return the minimum shape — anything else
        # would be lying about response stability.
        return {"id": 10_000 + len(self.replies)}

    async def add_label(self, event: UnifiedEvent, *labels: str) -> list[dict[str, Any]]:
        self.labels_added.append((event.repo, labels))
        return [{"name": label} for label in labels]

    async def remove_label(self, event: UnifiedEvent, label: str) -> None:
        self.labels_removed.append((event.repo, label))

    async def get_actor_role(self, event: UnifiedEvent, login: str | None = None) -> str:
        # Default "admin" so happy-path demos that don't set roles still
        # PROCEED past the fix/chat actor-role gate. Tests that exercise
        # the negative path explicitly assign ``actor_roles[actor]``.
        target = login if login is not None else event.actor
        return self.actor_roles.get(target, "admin")

    async def get_issue_labels(self, event: UnifiedEvent, number: int) -> frozenset[str]:
        """Return label names from the test-controlled labels_response."""
        return frozenset(
            str(item["name"])
            for item in self.labels_response
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )

    async def get_pr_comments(self, event: UnifiedEvent, pr_number: int) -> list[dict[str, Any]]:
        """Return the test-controlled comments list."""
        return list(self.comments_response)

    async def read_file(self, event: UnifiedEvent, path: str) -> str:
        """Return test-controlled file content keyed by path; '' if unset."""
        return self.file_responses.get(path, "")

    async def grep_repo(
        self,
        event: UnifiedEvent,
        *,
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        """Return canned grep results keyed by (pattern, path_glob)."""
        return list(self.grep_responses.get((pattern, path_glob), ()))[:max_matches]

    async def _installation_token(self, event: UnifiedEvent) -> Any:
        """Bypass App auth — return a minimal object whose ``.token`` is read."""

        class _Tok:
            token = "fake-installation-token"

        return _Tok()

    async def _authed_json(
        self,
        method: str,
        url: str,
        event: UnifiedEvent,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Stand in for the real HTTP call.

        We route on URL substring (the real adapter builds urls of the form
        ``/repos/{owner}/{repo}/issues/{n}/labels`` etc.). Returning ``None``
        for unrecognised paths lets the test surface unexpected callouts.
        """
        if method == "GET" and url.endswith("/labels"):
            # CancelLabelMiddleware label-list fetch.
            return self.labels_response
        if method == "GET" and "/comments" in url:
            # ForkPRGateMiddleware PR-comment fetch.
            return self.comments_response
        if method == "GET" and "/permission" in url:
            # Fork-gate role-lookup for *another* actor (the commenter).
            # The url has the form .../collaborators/{login}/permission.
            login = url.rsplit("/", 2)[-2]
            return {"permission": self.actor_roles.get(login, "none")}
        # Workflow handlers should call .reply(), not _authed_json. Anything
        # else is an unexpected egress — surface it loudly.
        raise AssertionError(f"unexpected _authed_json {method} {url}")


@dataclass(slots=True)
class WebhookHarness:
    """One e2e env: adapter + redis + sqlite + dispatch helper."""

    adapter: RecordingGitHubAdapter
    redis: fakeredis.aioredis.FakeRedis
    session_factory: async_sessionmaker[AsyncSession]
    # Effective config the (monkeypatched) ``load_for_repo`` returns. Tests
    # mutate ``harness.config = replace(harness.config, ...)`` to dial
    # specific knobs (rate limit threshold for demo 8 etc.) without going
    # through the real GitHub Contents API.
    config: EffectiveConfig = field(default_factory=baked_in_defaults)
    # Test-controlled fields the demos assemble per call.
    repo: str = "acme/test-repo"
    installation_id: int = 99
    raw_overrides: dict[str, Any] = field(default_factory=dict)

    def make_event(
        self,
        *,
        kind: EventKind,
        actor: str = "alice",
        actor_type: str | None = "User",
        delivery_id: str = "deliv-1",
        issue_number: int | None = 7,
        pr_number: int | None = None,
        comment_body: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> UnifiedEvent:
        """Synthesize a ``UnifiedEvent`` mirroring what GitHubAdapter would parse."""
        return UnifiedEvent(
            channel="github",
            delivery_id=delivery_id,
            kind=kind,
            repo=self.repo,
            actor=actor,
            actor_type=actor_type,
            issue_number=issue_number,
            pr_number=pr_number,
            comment_body=comment_body,
            installation_id=self.installation_id,
            raw=raw or {},
        )

    async def dispatch(self, event: UnifiedEvent) -> None:
        """Run the full pre-flight chain + workflow handler for ``event``.

        Mirrors what the worker does after popping an entry from the queue.
        ``dispatch_for`` may return ``None`` (router decides it's not relevant);
        we treat that as a no-op so tests can pass irrelevant events without
        a special-case branch.
        """
        decision: Dispatch | None = dispatch_for(event)
        if decision is None:
            return
        await run_dispatch(
            adapter=self.adapter,
            event=event,
            dispatch=decision,
            session_factory=self.session_factory,
            redis=self.redis,
            rate_limiter=RedisRateLimiter(self.redis),
        )

    async def audit_rows(self, *, delivery_id: str | None = None) -> Sequence[AuditLog]:
        """All audit_log rows, optionally filtered by ``delivery_id``."""
        async with self.session_factory() as session:
            stmt = select(AuditLog).order_by(AuditLog.created_at.asc())
            if delivery_id is not None:
                stmt = stmt.where(AuditLog.delivery_id == delivery_id)
            result = await session.execute(stmt)
            return list(result.scalars())


@pytest.fixture
async def webhook_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[WebhookHarness]:
    """Composes adapter + fakeredis + sqlite-memory for one test.

    Patches ``openbot.application.dispatcher.load_for_repo`` so each dispatch reads
    ``harness.config`` instead of hitting api.github.com for the YAML
    file. Tests override fields via ``dataclasses.replace`` on the
    frozen ``EffectiveConfig``.

    Demos 1-9 assert against the real workflow handlers
    (``maybe_run_triage`` / ``_review`` / ``_fix`` / ``_chat``); the
    process-wide default ``OPENBOT_DEBUG_ECHO=1`` would route them to
    the debug-echo handler instead. Disable here so the demos see
    the workflow callables they originally targeted.
    """
    from openbot.core.settings import get_settings

    monkeypatch.setenv("OPENBOT_DEBUG_ECHO_ENABLED", "false")
    get_settings.cache_clear()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    adapter = RecordingGitHubAdapter()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    harness = WebhookHarness(adapter=adapter, redis=redis, session_factory=session_factory)

    # The real loader reaches out to GitHub over httpx. In tests we just
    # hand back whatever the test put on ``harness.config`` (frozen
    # dataclass — mutation requires ``dataclasses.replace``).
    async def _fake_load_for_repo(_adapter: Any, _event: UnifiedEvent) -> EffectiveConfig:
        return harness.config

    monkeypatch.setattr("openbot.application.dispatcher.load_for_repo", _fake_load_for_repo)

    async def _fake_chat_reply(*, event: UnifiedEvent, user_request: str) -> str:
        return f"DeepAgents test reply: {user_request}"

    monkeypatch.setattr(
        "openbot.application.use_cases.chat._generate_freeform_reply",
        _fake_chat_reply,
    )

    async def _fake_review_reply(*, event: UnifiedEvent, adapter: Any) -> str:
        # E2E doesn't exercise the real reviewer — only the audit + reply
        # plumbing. PRD §8.3: prompt-quality assertions live in evals/.
        return f"DeepAgents review reply for PR #{event.pr_number}"

    monkeypatch.setattr(
        "openbot.application.use_cases.review._generate_review_reply",
        _fake_review_reply,
    )

    try:
        yield harness
    finally:
        await adapter.aclose()
        await redis.aclose()
        await engine.dispose()

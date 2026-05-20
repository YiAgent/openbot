"""E2E demos for decide_and_enqueue() — the webhook async segment.

Covers the new F1/F2/F3 webhook-worker-layering behavior end-to-end:

  D1-D9: preflight chain (config, middleware)
  D11:   EventContext extraction (pure)
  D12:   Direct-action short-circuit (empty body / oversized PR / vague mention)
  D10:   LLM classifier → stages_to_run
  D13b:  Incremental PR scope (last_reviewed_sha → is_incremental / is_force_push)
  D13:   TaskSpec v3 build and enqueue

Execution model: calls ``decide_and_enqueue()`` directly with:
  - FakeChannelAdapter (records replies + labels)
  - FakeConfigLoader   (returns baked-in defaults)
  - FakeQueue          (collects task_specs in memory)
  - fakeredis          (for rate-limiter + cache path)
  - aiosqlite in-memory DB (for last_reviewed_sha lookup)

Each test is a mini acceptance demo, named after the spec §8 acceptance criteria.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from openbot.dispatcher import decide_and_enqueue
from openbot.dispatcher.classifier import ReviewClassifierOutput, TriageClassifierOutput
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.persistence.models import Base, TaskRun
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader
from tests._fakes.queue import FakeQueue

# ── Constants ────────────────────────────────────────────────────────────────

_REPO = "org/smoke-repo"
_ACTOR = "alice"
_INSTALL = 42


# ── Event factories ───────────────────────────────────────────────────────────


def _issue_event(
    *,
    body: str | None = "This is a real bug: steps to reproduce are X, Y, Z.",
    delivery: str = "del-e2e-issue",
    issue_number: int = 10,
    labels: list[dict] | None = None,
) -> UnifiedEvent:
    """Create a realistic ISSUE_OPENED event."""
    issue_raw: dict[str, Any] = {
        "number": issue_number,
        "title": "E2E smoke test issue",
        "state": "open",
        "user": {"login": _ACTOR, "type": "User"},
        "labels": labels or [],
    }
    if body is not None:
        issue_raw["body"] = body
    return UnifiedEvent(
        channel="github",
        delivery_id=delivery,
        kind=EventKind.ISSUE_OPENED,
        repo=_REPO,
        actor=_ACTOR,
        actor_type="User",
        issue_number=issue_number,
        pr_number=None,
        installation_id=_INSTALL,
        comment_body=None,
        raw={
            "issue": issue_raw,
            "repository": {"full_name": _REPO},
            "installation": {"id": _INSTALL},
        },
    )


def _pr_event(
    *,
    delivery: str = "del-e2e-pr",
    pr_number: int = 20,
    head_sha: str = "head-sha-abc",
    base_sha: str = "base-sha-000",
    before_sha: str | None = None,
    additions: int = 100,
    deletions: int = 50,
    changed_files: int = 5,
    draft: bool = False,
) -> UnifiedEvent:
    """Create a realistic PR_OPENED / PR_SYNCHRONIZE event."""
    raw: dict[str, Any] = {
        "pull_request": {
            "number": pr_number,
            "title": "E2E smoke test PR",
            "body": "Small refactor to improve readability.",
            "state": "open",
            "draft": draft,
            "merged": False,
            "user": {"login": _ACTOR, "type": "User"},
            "head": {
                "sha": head_sha,
                "ref": "feature/e2e",
                "repo": {"full_name": _REPO, "fork": False},
            },
            "base": {"sha": base_sha, "ref": "main", "repo": {"full_name": _REPO}},
            "additions": additions,
            "deletions": deletions,
            "changed_files": changed_files,
            "labels": [],
        },
        "repository": {"full_name": _REPO},
        "installation": {"id": _INSTALL},
    }
    if before_sha is not None:
        raw["before"] = before_sha
    return UnifiedEvent(
        channel="github",
        delivery_id=delivery,
        kind=EventKind.PR_OPENED,
        repo=_REPO,
        actor=_ACTOR,
        actor_type="User",
        issue_number=None,
        pr_number=pr_number,
        installation_id=_INSTALL,
        comment_body=None,
        raw=raw,
    )


def _comment_event(
    *,
    body: str,
    delivery: str = "del-e2e-comment",
    issue_number: int = 10,
) -> UnifiedEvent:
    """Create a realistic ISSUE_COMMENT_CREATED event (for @mention chat)."""
    return UnifiedEvent(
        channel="github",
        delivery_id=delivery,
        kind=EventKind.ISSUE_COMMENT_CREATED,
        repo=_REPO,
        actor=_ACTOR,
        actor_type="User",
        issue_number=issue_number,
        pr_number=None,
        installation_id=_INSTALL,
        comment_body=body,
        raw={
            "issue": {"number": issue_number, "state": "open", "user": {"login": _ACTOR}},
            "comment": {"body": body, "user": {"login": _ACTOR, "type": "User"}},
            "repository": {"full_name": _REPO},
            "installation": {"id": _INSTALL},
        },
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def fake_redis() -> AsyncGenerator[fakeredis.aioredis.FakeRedis, None]:
    """In-memory Redis instance for rate-limiter + classifier cache."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    yield client
    await client.aclose()


@pytest.fixture
async def db_session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """In-memory SQLite session factory with the openbot schema.

    Uses yield so the engine is properly disposed after each test, preventing
    aiosqlite ResourceWarning about unclosed connections leaking across tests.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _plant_reviewed_sha(
    session_factory: async_sessionmaker[AsyncSession],
    resource_key: str,
    sha: str,
) -> None:
    """Seed the DB with a task_runs row carrying last_reviewed_sha."""
    async with session_factory() as session, session.begin():
        row = TaskRun(
            resource_key=resource_key,
            last_reviewed_sha=sha,
        )
        session.add(row)


async def _run(
    event: UnifiedEvent,
    *,
    queue: FakeQueue,
    adapter: FakeChannelAdapter,
    fake_redis: fakeredis.aioredis.FakeRedis,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Thin wrapper: dispatch_for → decide_and_enqueue."""
    from openbot.application.router import dispatch_for

    dispatch = dispatch_for(event)
    assert dispatch is not None, f"No handler registered for {event.kind}"
    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=session_factory,
        redis=fake_redis,
    )


# ══════════════════════════════════════════════════════════════════════════════
# F2 — Direct-action short-circuit demos
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_da_01_empty_issue_body_triggers_needs_info(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F2 demo: empty issue body → bot posts needs-info reply + label, no enqueue.

    Spec §8 F2: "open an empty body issue → bot doesn't enter worker queue,
    only sends needs-info comment".
    """
    event = _issue_event(body="", delivery="da-01")
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    # Direct-action: reply was posted
    assert len(adapter.replies) == 1, "Expected exactly one reply for empty body"
    _, reply_msg = adapter.replies[0]
    assert "needs-info" in reply_msg.lower() or "description" in reply_msg.lower(), (
        f"Expected needs-info message, got: {reply_msg!r}"
    )

    # Direct-action: "needs-info" label was added
    assert len(adapter.labels_added) == 1, "Expected label to be added"
    _, labels = adapter.labels_added[0]
    assert "needs-info" in labels, f"Expected 'needs-info' label, got: {labels}"

    # No TaskSpec enqueued (short-circuit before enqueue)
    assert len(queue.task_specs) == 0, (
        f"Direct-action must NOT enqueue; found {len(queue.task_specs)} spec(s)"
    )


@pytest.mark.asyncio
async def test_da_02_substantive_issue_body_proceeds_to_queue(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F2 demo: substantive issue body → no direct-action, TaskSpec v3 enqueued."""
    event = _issue_event(
        body="I found a bug when clicking the submit button. Steps: 1) go to /form 2) fill name 3) click submit. Expected: success. Actual: 500 error.",
        delivery="da-02",
    )
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    # No direct-action reply
    assert len(adapter.replies) == 0, (
        f"Substantive body should NOT trigger direct-action; got {len(adapter.replies)} replies"
    )

    # TaskSpec v3 was enqueued
    assert len(queue.task_specs) == 1, f"Expected 1 TaskSpec, got {len(queue.task_specs)}"
    spec = queue.task_specs[0]
    assert spec.spec_version == 3
    assert spec.repo == _REPO
    assert spec.scenario == "triage"


@pytest.mark.asyncio
async def test_da_03_oversized_pr_triggers_split_message(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F2 demo: PR with >500 lines changed → direct-action, no enqueue.

    Spec §8 F2: "open an 800-line PR → bot doesn't enter queue, sends
    'consider splitting' comment".
    """
    # 600 additions + 200 deletions = 800 lines total > PR_OVERSIZED_THRESHOLD (500)
    event = _pr_event(additions=600, deletions=200, changed_files=25, delivery="da-03")
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    # Reply posted with size info
    assert len(adapter.replies) == 1, "Expected one reply for oversized PR"
    _, reply_msg = adapter.replies[0]
    assert "800" in reply_msg or "500" in reply_msg, (
        f"Expected line count in message, got: {reply_msg!r}"
    )
    assert "split" in reply_msg.lower() or "smaller" in reply_msg.lower(), (
        f"Expected split recommendation, got: {reply_msg!r}"
    )

    # No enqueue
    assert len(queue.task_specs) == 0, "Oversized PR must not be enqueued"


@pytest.mark.asyncio
async def test_da_04_normal_pr_proceeds_to_queue(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F2 demo: PR within size threshold → no direct-action, TaskSpec v3 enqueued."""
    event = _pr_event(additions=80, deletions=20, changed_files=3, delivery="da-04")
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    assert len(adapter.replies) == 0, "Normal PR should not trigger direct-action"
    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.spec_version == 3
    assert spec.scenario == "review"


@pytest.mark.asyncio
async def test_da_05_vague_mention_triggers_clarification(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F2 demo: too-short @mention → direct-action clarification, no enqueue.

    Spec §8 F2: "@openbot 改一下吧 → bot directly asks 'what do you want to change',
    doesn't enter queue".
    """
    # "改一下吧" is about 5 chars — well below _MENTION_MIN_CHARS (20)
    event = _comment_event(body="@openbot fix it", delivery="da-05")
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    assert len(adapter.replies) == 1, "Vague mention should trigger clarification reply"
    _, reply_msg = adapter.replies[0]
    # Message should ask for more detail
    assert any(w in reply_msg.lower() for w in ("short", "detail", "describe", "example", "try")), (
        f"Expected clarification prompt, got: {reply_msg!r}"
    )
    assert len(queue.task_specs) == 0, "Vague mention must not be enqueued"


@pytest.mark.asyncio
async def test_da_06_clear_mention_proceeds_to_queue(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F2 demo: substantive @mention → no direct-action, TaskSpec v3 enqueued."""
    # Long enough to pass the _MENTION_MIN_CHARS check (>20 chars after prefix)
    event = _comment_event(
        body="@openbot can you review the authentication module for security issues and let me know if there are any vulnerabilities?",
        delivery="da-06",
    )
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    assert len(adapter.replies) == 0, "Clear mention should not trigger direct-action"
    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.spec_version == 3
    assert spec.scenario == "chat"


# ══════════════════════════════════════════════════════════════════════════════
# F1 — TaskSpec v3 structure demos
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_spec_v3_01_version_and_fields(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F1: Every enqueued spec is TaskSpec v3 with required fields populated."""
    event = _issue_event(delivery="spec-01")
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]

    assert spec.spec_version == 3, f"spec_version must be 3, got {spec.spec_version}"
    assert spec.repo == _REPO
    assert spec.actor == _ACTOR
    assert spec.delivery_id == "spec-01"
    assert spec.channel == "github"
    assert spec.kind is not None
    assert spec.installation_id == _INSTALL
    assert spec.enqueued_at is not None
    assert spec.spec_built_at is not None
    assert spec.task_id is not None and len(spec.task_id) > 0
    # initial_labels extracted from raw payload (empty list here)
    assert isinstance(spec.initial_labels, list)


@pytest.mark.asyncio
async def test_spec_v3_02_pr_spec_has_pr_number(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F1: PR events produce a spec with pr_number populated."""
    event = _pr_event(pr_number=42, delivery="spec-02")
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.pr_number == 42
    assert spec.issue_number is None
    assert spec.scenario == "review"


@pytest.mark.asyncio
async def test_spec_v3_03_initial_labels_snapshot(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F1: initial_labels carries the W1 cancel-check snapshot from the raw payload."""
    event = _issue_event(
        labels=[{"id": 1, "name": "bug"}, {"id": 2, "name": "triage"}],
        delivery="spec-03",
    )
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert "bug" in spec.initial_labels, f"'bug' not in initial_labels: {spec.initial_labels}"
    assert "triage" in spec.initial_labels


# ══════════════════════════════════════════════════════════════════════════════
# F3 — LLM classifier output demos
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_f3_01_classifier_failopen_when_no_credentials(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F3: When LLM credentials are absent, classifier fails open.

    Spec §6.4: "any exception → classifier_skipped=True, worker按全套 stages 跑".
    The spec is still built and enqueued — fail-open is graceful degradation.
    """
    event = _issue_event(delivery="f3-01")
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    # No LLM credentials in test environment → classifier will raise → fail-open
    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    # Fail-open: classifier_skipped=True, spec still usable
    assert spec.classifier_skipped is True, (
        "Without LLM credentials, classifier_skipped must be True"
    )
    # classifier_output is None when skipped
    assert spec.classifier_output is None


@pytest.mark.asyncio
async def test_f3_02_mocked_triage_classifier_populates_spec(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F3: When classifier succeeds, output is stored in TaskSpec.classifier_output."""
    event = _issue_event(delivery="f3-02")
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    mock_output = TriageClassifierOutput(
        type="bug",
        severity_guess="high",
        has_reproduction_info=True,
        looks_like_spam=False,
    )

    with patch("openbot.dispatcher.decide.classify_event", new=AsyncMock(return_value=mock_output)):
        await _run(
            event,
            queue=queue,
            adapter=adapter,
            fake_redis=fake_redis,
            session_factory=db_session_factory,
        )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.classifier_skipped is False, "Mocked classifier succeeded → skipped must be False"
    assert spec.classifier_output is not None, "classifier_output must be populated"
    assert spec.classifier_output["type"] == "bug"
    assert spec.classifier_output["severity_guess"] == "high"
    assert spec.classifier_output["has_reproduction_info"] is True
    assert spec.classifier_output["looks_like_spam"] is False


@pytest.mark.asyncio
async def test_f3_03_mocked_review_classifier_sets_stages(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F3: Review classifier output drives stages_to_run.

    Spec §8 F3: "review on security-sensitive PR → add security sub-agent".
    """
    event = _pr_event(delivery="f3-03", additions=80, deletions=20)
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    mock_output = ReviewClassifierOutput(
        change_size_class="s",
        touches_security_paths=True,
        is_breaking=False,
        suggested_subagents=("correctness", "security"),
    )

    with patch("openbot.dispatcher.decide.classify_event", new=AsyncMock(return_value=mock_output)):
        await _run(
            event,
            queue=queue,
            adapter=adapter,
            fake_redis=fake_redis,
            session_factory=db_session_factory,
        )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert "security" in spec.stages_to_run, (
        f"Security-sensitive PR must have 'security' stage; got {spec.stages_to_run}"
    )
    assert "correctness" in spec.stages_to_run, (
        f"Expected 'correctness' stage; got {spec.stages_to_run}"
    )


@pytest.mark.asyncio
async def test_f3_04_mocked_docs_only_pr_stages(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F3: Docs-only PR → stages_to_run = ['docs'].

    Spec §8 F3: "review on docs-only PR → spec.stages_to_run = ['docs']".
    """
    event = _pr_event(delivery="f3-04", additions=30, deletions=5)
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    mock_output = ReviewClassifierOutput(
        change_size_class="xs",
        touches_security_paths=False,
        is_breaking=False,
        suggested_subagents=("docs",),
    )

    with patch("openbot.dispatcher.decide.classify_event", new=AsyncMock(return_value=mock_output)):
        await _run(
            event,
            queue=queue,
            adapter=adapter,
            fake_redis=fake_redis,
            session_factory=db_session_factory,
        )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.stages_to_run == ["docs"], (
        f"Docs-only PR should have stages=['docs'], got {spec.stages_to_run}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# F3 — Incremental PR review demos
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_f3_05_first_pr_review_not_incremental(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F3: First review of a PR (no last_reviewed_sha) → is_incremental=False.

    Spec §8 F3: "second push → spec.is_incremental=true" (implies first is False).
    """
    event = _pr_event(delivery="f3-05", head_sha="sha-v1")
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    # No seed in DB → get_last_reviewed_sha returns None
    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.is_incremental is False, "First review must not be incremental"
    assert spec.is_force_push is False, "First review must not be force-push"


@pytest.mark.asyncio
async def test_f3_06_second_push_is_incremental(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F3: Second push where before_sha == last_reviewed_sha → is_incremental=True.

    Spec §8 F3: "第二次 push → spec.is_incremental=true, diff_scope 缩小到增量".
    """
    _RESOURCE_KEY = f"github:{_REPO}:pr:20"
    _LAST_SHA = "sha-v1-reviewed"

    # Seed: previous review completed and stored last_reviewed_sha
    await _plant_reviewed_sha(db_session_factory, _RESOURCE_KEY, _LAST_SHA)

    # PR synchronize: before = last reviewed SHA (linear history).
    # resource_key is a @property computed from channel/repo/pr_number;
    # pr_number=20 + repo=_REPO → event.resource_key == _RESOURCE_KEY automatically.
    event = _pr_event(
        delivery="f3-06",
        head_sha="sha-v2",
        before_sha=_LAST_SHA,  # before == last_reviewed_sha → incremental
    )
    assert event.resource_key == _RESOURCE_KEY, "Sanity: property matches seeded key"
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.is_incremental is True, (
        f"Second push with matching SHA must be incremental; got is_incremental={spec.is_incremental}"
    )
    assert spec.is_force_push is False, "Linear push must not be force-push"


@pytest.mark.asyncio
async def test_f3_07_force_push_triggers_full_review(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F3: Force-push (before_sha ≠ last_reviewed_sha) → is_force_push=True.

    Spec §8 F3: "force-push → spec.is_force_push=true, 退回全量".
    """
    _RESOURCE_KEY = f"github:{_REPO}:pr:20"
    _LAST_SHA = "sha-v1-reviewed"

    await _plant_reviewed_sha(db_session_factory, _RESOURCE_KEY, _LAST_SHA)

    # Force-push: before_sha is something else (history rewritten).
    # resource_key is a @property computed from channel/repo/pr_number automatically.
    event = _pr_event(
        delivery="f3-07",
        head_sha="sha-rebase",
        before_sha="sha-rebase-old",  # ≠ last_reviewed_sha → force-push
    )
    assert event.resource_key == _RESOURCE_KEY, "Sanity: property matches seeded key"
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.is_force_push is True, (
        f"Force-push must set is_force_push=True; got {spec.is_force_push}"
    )
    assert spec.is_incremental is False, "Force-push must not be incremental"


# ══════════════════════════════════════════════════════════════════════════════
# F-series acceptance tests (from spec §7.2)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_f01_classifier_timeout_fails_open(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F-01: Classifier exception → spec.classifier_skipped=True, worker gets full stages.

    Spec §7.2: "F-01: webhook 异步段 LLM classifier 超时 → spec.classifier_skipped=true,
    worker 按全套 stages 跑".
    """

    event = _pr_event(delivery="f01-timeout")
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    async def _timeout(*_a, **_kw):
        raise TimeoutError("simulated LLM timeout")

    with patch("openbot.dispatcher.decide.classify_event", new=_timeout):
        await _run(
            event,
            queue=queue,
            adapter=adapter,
            fake_redis=fake_redis,
            session_factory=db_session_factory,
        )

    assert len(queue.task_specs) == 1, "Timeout must not prevent enqueue"
    spec = queue.task_specs[0]
    assert spec.classifier_skipped is True, "Timeout → classifier_skipped must be True"
    assert spec.classifier_output is None


@pytest.mark.asyncio
async def test_f02_direct_action_never_enqueues(
    fake_redis: fakeredis.aioredis.FakeRedis,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F-02: Direct-action path → no task enters queue, audit only shows SKIPPED-like row.

    Spec §7.2: "F-02: webhook 直接落地 → 没有 task 入 queue".
    We verify: reply posted, zero specs enqueued.
    """
    # Empty body → triggers direct-action
    event = _issue_event(body="", delivery="f02-direct")
    queue = FakeQueue()
    adapter = FakeChannelAdapter()

    await _run(
        event,
        queue=queue,
        adapter=adapter,
        fake_redis=fake_redis,
        session_factory=db_session_factory,
    )

    # Verify the contract: reply exists, queue empty
    assert len(adapter.replies) >= 1, "Direct-action must post a reply"
    assert len(queue.task_specs) == 0, (
        "F-02 contract: direct-action path must NEVER enqueue a TaskSpec"
    )

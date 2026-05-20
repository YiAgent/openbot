"""Security middlewares — Fork PR gate + Actor role gate.

Plan-ID coverage map (``docs/_archive/webhook-worker/webhook-worker-test-plan.md`` Phase 1):

  P-20  Fork PR triggers review → only static analysis (no sandbox).
        ``test_fork_pr_blocks_without_ok_to_test`` — the gate is what
        stops sandbox-bound work; review then runs against payload diff.

  P-21  Fork PR triggers fix → entrance rejected.
        ``test_fork_pr_skips_non_review_features`` — only REVIEW is the
        gated PR-event feature here, and FIX in the v0.1 router is
        routed only from ISSUE_ASSIGNED, which a fork PR never produces.

  P-22  Fork PR @mention → routes through chat, not fix.
        ``test_fork_pr_skips_non_review_features`` + the chat workflow's
        own tool-allowlist (sandbox/git-write excluded).

  P-23  Fork PR sandbox env vars — sandbox isn't wired in v0.1; ADR
        defers to v0.2. The gate above is the primary defense.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from openbot.application.middleware import MiddlewareResult
from openbot.application.middleware.security import (
    ActorRoleMiddleware,
    ForkPRGateMiddleware,
)
from openbot.domain.events import EventKind
from openbot.infrastructure.llm.model_router import Feature
from tests.application.middleware.conftest import make_ctx, make_event


def _adapter_with_role(role: str = "none") -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_actor_role = AsyncMock(return_value=role)
    adapter.get_pr_comments = AsyncMock(return_value=[])
    return adapter


def _pr_event(
    *,
    head_full: str = "fork-org/openbot",
    base_full: str = "YiAgent/openbot",
    head_fork: bool | None = True,
) -> Any:
    """PR event with explicit head/base shape — covers both fork signals."""
    head_repo: dict[str, Any] = {"full_name": head_full}
    if head_fork is not None:
        head_repo["fork"] = head_fork
    return make_event(
        kind=EventKind.PR_OPENED,
        repo=base_full,
        pr_number=42,
        issue_number=None,
        raw={
            "pull_request": {
                "head": {"repo": head_repo},
                "base": {"repo": {"full_name": base_full}},
            }
        },
    )


# ───── ForkPRGateMiddleware ─────


async def test_fork_pr_passes_when_head_matches_base() -> None:
    """Same-repo PR is not a fork; PROCEED without API calls."""
    event = _pr_event(head_full="YiAgent/openbot", head_fork=False)
    adapter = _adapter_with_role()
    decision = await ForkPRGateMiddleware()(
        make_ctx(event=event, feature=Feature.REVIEW, adapter=adapter)
    )
    assert decision.result is MiddlewareResult.PROCEED
    # No need to fetch comments for same-repo PRs.
    adapter.get_pr_comments.assert_not_called()


async def test_fork_pr_blocks_without_ok_to_test() -> None:
    """Fork PR + no maintainer comment → BLOCKED + announce_key dedup."""
    event = _pr_event()
    adapter = _adapter_with_role()
    # No comments on the PR (already default in _adapter_with_role).
    decision = await ForkPRGateMiddleware()(
        make_ctx(event=event, feature=Feature.REVIEW, adapter=adapter)
    )
    assert decision.result is MiddlewareResult.BLOCKED
    assert decision.reason == "fork_pr_no_ok_to_test"
    assert decision.announce_key is not None  # 30-day dedup
    assert decision.comment is not None
    assert "/ok-to-test" in decision.comment


async def test_fork_pr_allows_when_maintainer_writes_ok_to_test() -> None:
    """A maintainer comment with the opt-in phrase → PROCEED."""
    event = _pr_event()
    adapter = _adapter_with_role()
    # Two port-method calls happen here:
    #   1) get_pr_comments → list of comments
    #   2) get_actor_role(event, login="maintainer") → role
    adapter.get_pr_comments = AsyncMock(
        return_value=[{"body": "/ok-to-test please run", "user": {"login": "maintainer"}}]
    )
    adapter.get_actor_role = AsyncMock(return_value="maintain")
    decision = await ForkPRGateMiddleware()(
        make_ctx(event=event, feature=Feature.REVIEW, adapter=adapter)
    )
    assert decision.result is MiddlewareResult.PROCEED


async def test_fork_pr_rejects_ok_to_test_from_non_maintainer() -> None:
    """Outside contributor can't self-greenlight their own fork PR."""
    event = _pr_event()
    adapter = _adapter_with_role()
    adapter.get_pr_comments = AsyncMock(
        return_value=[{"body": "/ok-to-test", "user": {"login": "random-user"}}]
    )
    adapter.get_actor_role = AsyncMock(return_value="read")  # NOT maintainer-equivalent
    decision = await ForkPRGateMiddleware()(
        make_ctx(event=event, feature=Feature.REVIEW, adapter=adapter)
    )
    assert decision.result is MiddlewareResult.BLOCKED
    assert decision.reason == "fork_pr_no_ok_to_test"


async def test_fork_pr_skips_non_review_features() -> None:
    """Only review dispatches reach this gate; triage/fix/chat PROCEED."""
    event = _pr_event()
    decision = await ForkPRGateMiddleware()(
        make_ctx(event=event, feature=Feature.TRIAGE, adapter=_adapter_with_role())
    )
    assert decision.result is MiddlewareResult.PROCEED


async def test_fork_pr_skips_non_pr_event_kinds() -> None:
    event = make_event(kind=EventKind.ISSUE_OPENED)
    decision = await ForkPRGateMiddleware()(
        make_ctx(event=event, feature=Feature.REVIEW, adapter=_adapter_with_role())
    )
    assert decision.result is MiddlewareResult.PROCEED


async def test_fork_pr_detects_fork_via_full_name_diff_only() -> None:
    """`head.repo.fork` may be absent; full_name mismatch alone is enough."""
    event = _pr_event(head_full="other-org/openbot", head_fork=None)
    adapter = _adapter_with_role()
    # No comments → no ok-to-test → BLOCKED (already default in _adapter_with_role).
    decision = await ForkPRGateMiddleware()(
        make_ctx(event=event, feature=Feature.REVIEW, adapter=adapter)
    )
    assert decision.result is MiddlewareResult.BLOCKED


# ───── ActorRoleMiddleware ─────


async def test_actor_role_blocks_fix_for_random_user() -> None:
    event = make_event(kind=EventKind.ISSUE_ASSIGNED)
    decision = await ActorRoleMiddleware()(
        make_ctx(event=event, feature=Feature.FIX, adapter=_adapter_with_role(role="read"))
    )
    assert decision.result is MiddlewareResult.BLOCKED
    assert decision.reason == "actor_role_not_allowed_fix"
    # No reply — silent drop (don't teach attackers which roles are close).
    assert decision.comment is None


@pytest.mark.parametrize("role", ["collaborator", "owner", "admin", "maintain", "write"])
async def test_actor_role_allows_fix_for_maintainer_roles(role: str) -> None:
    event = make_event(kind=EventKind.ISSUE_ASSIGNED)
    decision = await ActorRoleMiddleware()(
        make_ctx(event=event, feature=Feature.FIX, adapter=_adapter_with_role(role=role))
    )
    assert decision.result is MiddlewareResult.PROCEED, role


async def test_actor_role_passes_chat_when_allow_anyone_default() -> None:
    """Default config has allow_anyone=True; chat is open to everyone."""
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED)
    decision = await ActorRoleMiddleware()(
        make_ctx(event=event, feature=Feature.CHAT, adapter=_adapter_with_role(role="none"))
    )
    assert decision.result is MiddlewareResult.PROCEED


async def test_actor_role_blocks_chat_when_allow_anyone_false() -> None:
    """Workspace can opt into restricted chat by setting allow_anyone: false."""
    from dataclasses import replace

    from openbot.infrastructure.config_loader import baked_in_defaults

    # Build a config with chat.allow_anyone explicitly False in raw.
    base = baked_in_defaults()
    restricted = replace(base, raw={"chat": {"allow_anyone": False}})

    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED)
    ctx = make_ctx(
        event=event,
        feature=Feature.CHAT,
        adapter=_adapter_with_role(role="none"),
        config=restricted,
    )
    decision = await ActorRoleMiddleware()(ctx)
    assert decision.result is MiddlewareResult.BLOCKED
    assert decision.reason == "actor_role_not_allowed_chat"


async def test_actor_role_passes_triage_and_review() -> None:
    """Impersonal triggers are governed by other gates, not actor role."""
    for feature in (Feature.TRIAGE, Feature.REVIEW):
        decision = await ActorRoleMiddleware()(
            make_ctx(feature=feature, adapter=_adapter_with_role(role="none"))
        )
        assert decision.result is MiddlewareResult.PROCEED, feature


async def test_actor_role_caches_lookup_on_ctx() -> None:
    """One actor-role API call per webhook even across middlewares."""
    event = make_event(kind=EventKind.ISSUE_ASSIGNED)
    adapter = _adapter_with_role(role="write")
    ctx = make_ctx(event=event, feature=Feature.FIX, adapter=adapter)
    await ActorRoleMiddleware()(ctx)
    await ActorRoleMiddleware()(ctx)
    assert adapter.get_actor_role.await_count == 1

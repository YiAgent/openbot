"""Regression test: preflight-blocked path must finalize the GitHub check run.

Before the fix, _execute_task_spec returned early on a BLOCKED preflight without
calling adapter.update_check_run(), leaving the "OpenBot Analysis" check run
stuck in "in_progress" on GitHub forever — blocking all CI status checks.

See: openbot/infrastructure/queue/worker.py — the preflight-blocked early return.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest

from openbot.application.middleware.preflight import MiddlewareDecision, MiddlewareResult
from openbot.application.router import Dispatch, SandboxPolicy
from openbot.domain.workflows import Feature
from openbot.infrastructure.config_loader import baked_in_defaults
from openbot.infrastructure.queue.task_spec import TaskSpec
from openbot.infrastructure.queue.worker import _execute_task_spec
from openbot.testing.builders.events import build_issue_opened_event
from openbot.testing.fakes import CheckRunRecord, FakeChannelAdapter


class _AlwaysBlockMiddleware:
    """Stub middleware that always returns BLOCKED — simulates kill-switch, budget cap, etc."""

    name = "always_block"

    async def __call__(self, ctx: object) -> MiddlewareDecision:
        return MiddlewareDecision(
            result=MiddlewareResult.BLOCKED,
            reason="test_preflight_block",
        )


async def _noop_handler(ctx: object) -> None:
    pass


def _make_spec(check_run_id: int | None) -> tuple[TaskSpec, Dispatch]:
    event = build_issue_opened_event(issue_number=1)
    dispatch = Dispatch(
        feature=Feature.TRIAGE,
        handler=_noop_handler,
        task_id="a" * 32,
        sandbox_policy=SandboxPolicy.NO_SANDBOX,
    )
    spec = TaskSpec.from_event_and_dispatch(event, dispatch, check_run_id=check_run_id)
    return spec, dispatch


@pytest.mark.integration
class TestPreflightBlockedCheckRun:
    async def test_check_run_completed_as_skipped_when_preflight_blocks(self) -> None:
        """Check run must be finalized to 'skipped' when preflight blocks.

        This is a regression test for the bug where a BLOCKED preflight caused the
        'OpenBot Analysis' check to stay in_progress indefinitely.
        """
        spec, dispatch = _make_spec(check_run_id=42)
        adapter = FakeChannelAdapter()
        redis = fakeredis.aioredis.FakeRedis(decode_responses=False)

        try:
            with (
                patch(
                    "openbot.infrastructure.queue.worker.dispatch_for",
                    return_value=dispatch,
                ),
                patch(
                    "openbot.infrastructure.queue.worker.load_for_repo",
                    new=AsyncMock(return_value=baked_in_defaults()),
                ),
                patch(
                    "openbot.infrastructure.queue.worker.build_preflight_chain",
                    return_value=[_AlwaysBlockMiddleware()],
                ),
            ):
                await _execute_task_spec(
                    spec,
                    entry_id="1234567890-0",
                    redis=redis,
                    adapter=adapter,
                    session_factory=None,
                )
        finally:
            await redis.aclose()

        updates = adapter.check_run_updates
        assert len(updates) == 1
        assert updates[0] == CheckRunRecord(
            repo=spec.repo,
            check_run_id=42,
            status="completed",
            conclusion="skipped",
        )

    async def test_no_check_run_update_when_check_run_id_is_none(self) -> None:
        """When there is no check_run_id, no update call should be made on block."""
        spec, dispatch = _make_spec(check_run_id=None)
        adapter = FakeChannelAdapter()
        redis = fakeredis.aioredis.FakeRedis(decode_responses=False)

        try:
            with (
                patch(
                    "openbot.infrastructure.queue.worker.dispatch_for",
                    return_value=dispatch,
                ),
                patch(
                    "openbot.infrastructure.queue.worker.load_for_repo",
                    new=AsyncMock(return_value=baked_in_defaults()),
                ),
                patch(
                    "openbot.infrastructure.queue.worker.build_preflight_chain",
                    return_value=[_AlwaysBlockMiddleware()],
                ),
            ):
                await _execute_task_spec(
                    spec,
                    entry_id="1234567890-1",
                    redis=redis,
                    adapter=adapter,
                    session_factory=None,
                )
        finally:
            await redis.aclose()

        assert adapter.check_run_updates == ()

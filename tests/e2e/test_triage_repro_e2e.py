"""E2E tests for the triage reproduce pipeline — slice R5.

Exercises the full ``maybe_run_triage`` path end-to-end with fakes:

  webhook-event ──▶ PreflightContext ──▶ maybe_run_triage
                        │                     │
                  FakeChannelAdapter      monkeypatched
                  SandboxedHandle         _generate_repro_outcome
                  TriageClassifierOutput

No Docker, no real LLM, no network.  Docker E2E is deferred until the
local sandbox adapter lands in v0.2; that slice will extend these tests
with a real in-process sandbox container.

Design contract (per spec §R5.1):

  - Every triage + bug-with-repro path must post exactly ONE ``reply``
    (the thinking placeholder) followed by exactly ONE ``update_comment``
    (the rendered final body).
  - Non-reproduce paths (type ≠ bug, or no sandbox handle) post the same
    two calls but the final body is the ACK template (no status markers).
  - Agent failures land the AGENT_FAILED template on the same comment —
    no raw exception class names must appear in the rendered body.

Budget: tests must complete in < 2 s total (use-case layer — no I/O).
"""

from __future__ import annotations

import pytest

from openbot.application.middleware.preflight import PreflightContext
from openbot.application.router import Dispatch, derive_task_id
from openbot.application.sandbox_handle import SandboxedHandle
from openbot.application.use_cases.triage import maybe_run_triage
from openbot.dispatcher.classifier import TriageClassifierOutput
from openbot.domain.checkout import CheckoutSpec, CloneStrategy
from openbot.domain.repro import ReproOutcome, ReproStatus
from openbot.domain.workflows import Feature
from openbot.infrastructure.config_loader import baked_in_defaults
from openbot.testing.builders.events import build_issue_opened_event
from openbot.testing.fakes import FakeChannelAdapter, FakeSandbox

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INSTALL_ID = 200_000


def _bug_classifier(*, has_repro: bool = True) -> TriageClassifierOutput:
    return TriageClassifierOutput(
        type="bug",
        severity_guess="high",
        has_reproduction_info=has_repro,
        looks_like_spam=False,
    )


def _question_classifier() -> TriageClassifierOutput:
    return TriageClassifierOutput(
        type="question",
        severity_guess="low",
        has_reproduction_info=False,
        looks_like_spam=False,
    )


def _sandbox_handle() -> SandboxedHandle:
    return SandboxedHandle(
        sandbox=FakeSandbox(),
        checkout=CheckoutSpec(
            repo_url="https://github.com/owner/repo.git",
            ref="a" * 40,
            strategy=CloneStrategy.SHALLOW,
        ),
        token="fake-gh-token",
    )


def _reproduced_outcome() -> ReproOutcome:
    return ReproOutcome(
        status=ReproStatus.REPRODUCED,
        summary="Stack overflow in parse_args.",
        command="python repro.py",
        exit_code=1,
        output_excerpt="RecursionError: maximum recursion depth exceeded",
        hypothesis="parse_args calls itself without a base case",
        repro_artifact="#!/usr/bin/env python\npython repro.py\n",
        repro_artifact_filename="repro.sh",
    )


def _not_reproduced_outcome() -> ReproOutcome:
    return ReproOutcome(
        status=ReproStatus.NOT_REPRODUCED,
        summary="No crash observed with latest main.",
        command="python repro.py",
        exit_code=0,
        output_excerpt="Done.",
        hypothesis="May have been fixed in #123",
        repro_artifact=None,
        repro_artifact_filename=None,
    )


def _ctx(
    *,
    fake_channel: FakeChannelAdapter,
    classifier: TriageClassifierOutput | None,
    handle: SandboxedHandle | None,
    sender: str = "alice",
    issue_number: int = 42,
    repo: str = "owner/repo",
) -> PreflightContext:
    event = build_issue_opened_event(
        repo=repo,
        issue_number=issue_number,
        sender=sender,
        body="Steps to reproduce: run `python repro.py`",
        installation_id=_INSTALL_ID,
    )
    dispatch = Dispatch(
        feature=Feature.TRIAGE,
        handler=maybe_run_triage,
        task_id=derive_task_id(event),
    )
    return PreflightContext(
        event=event,
        dispatch=dispatch,
        config=baked_in_defaults(),
        adapter=fake_channel,
        session_factory=None,
        redis=None,
        classifier_output=classifier,
        sandbox_handle=handle,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestTriageReproHappyPath:
    async def test_reproduced_posts_thinking_then_checkmark(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """bug + repro_info + sandbox → thinking reply, then :white_check_mark: update."""
        fake_channel = FakeChannelAdapter()
        outcome = _reproduced_outcome()

        async def _fake_generate(_preflight_ctx: PreflightContext) -> ReproOutcome:
            return outcome

        monkeypatch.setattr(
            "openbot.application.use_cases.triage._generate_repro_outcome",
            _fake_generate,
        )

        ctx = _ctx(
            fake_channel=fake_channel,
            classifier=_bug_classifier(has_repro=True),
            handle=_sandbox_handle(),
        )

        await maybe_run_triage(ctx)

        # Exactly one initial reply (thinking placeholder)
        assert len(fake_channel.replies) == 1
        thinking_body = fake_channel.replies[0].message
        assert "@alice" in thinking_body
        assert "reproducing" in thinking_body.lower()

        # Exactly one comment update (final rendered body)
        assert len(fake_channel.comment_updates) == 1
        final_body = fake_channel.comment_updates[0].message
        assert ":white_check_mark:" in final_body
        assert "Reproduced" in final_body

    async def test_not_reproduced_posts_warning_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """bug + repro_info + sandbox → :warning: update when agent says NOT_REPRODUCED."""
        fake_channel = FakeChannelAdapter()

        async def _fake_generate(_preflight_ctx: PreflightContext) -> ReproOutcome:
            return _not_reproduced_outcome()

        monkeypatch.setattr(
            "openbot.application.use_cases.triage._generate_repro_outcome",
            _fake_generate,
        )

        ctx = _ctx(
            fake_channel=fake_channel,
            classifier=_bug_classifier(has_repro=True),
            handle=_sandbox_handle(),
        )

        await maybe_run_triage(ctx)

        assert len(fake_channel.replies) == 1
        assert len(fake_channel.comment_updates) == 1
        final_body = fake_channel.comment_updates[0].message
        assert ":warning:" in final_body
        assert "Could not reproduce" in final_body


@pytest.mark.e2e
class TestTriageReproSkipPaths:
    async def test_non_bug_posts_ack_only(self) -> None:
        """question event → thinking placeholder + ACK update (no reproduce)."""
        fake_channel = FakeChannelAdapter()

        ctx = _ctx(
            fake_channel=fake_channel,
            classifier=_question_classifier(),
            handle=None,  # no sandbox for non-bug
        )

        await maybe_run_triage(ctx)

        assert len(fake_channel.replies) == 1
        assert len(fake_channel.comment_updates) == 1
        final_body = fake_channel.comment_updates[0].message
        # ACK template — no reproduce markers
        assert "OpenBot received this issue" in final_body
        assert ":white_check_mark:" not in final_body
        assert ":warning:" not in final_body

    async def test_bug_without_repro_info_posts_ack(self) -> None:
        """bug + no repro_info → ACK update (policy returns NO_SANDBOX)."""
        fake_channel = FakeChannelAdapter()

        ctx = _ctx(
            fake_channel=fake_channel,
            classifier=_bug_classifier(has_repro=False),
            handle=None,
        )

        await maybe_run_triage(ctx)

        assert len(fake_channel.replies) == 1
        assert len(fake_channel.comment_updates) == 1
        final_body = fake_channel.comment_updates[0].message
        assert "OpenBot received this issue" in final_body

    async def test_bug_with_repro_info_but_no_sandbox_posts_ack(self) -> None:
        """Policy says REQUIRED but sandbox_handle is None (provisioning failed) → ACK."""
        fake_channel = FakeChannelAdapter()

        ctx = _ctx(
            fake_channel=fake_channel,
            classifier=_bug_classifier(has_repro=True),
            handle=None,  # provisioning failed
        )

        await maybe_run_triage(ctx)

        assert len(fake_channel.replies) == 1
        assert len(fake_channel.comment_updates) == 1
        final_body = fake_channel.comment_updates[0].message
        assert "OpenBot received this issue" in final_body


@pytest.mark.e2e
class TestTriageReproAgentFailure:
    async def test_agent_exception_renders_failed_template(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Agent raises → AGENT_FAILED comment, no exception class name in body."""
        fake_channel = FakeChannelAdapter()

        async def _fake_generate(_preflight_ctx: PreflightContext) -> ReproOutcome:
            raise RuntimeError("agent timed out after 180s")

        monkeypatch.setattr(
            "openbot.application.use_cases.triage._generate_repro_outcome",
            _fake_generate,
        )

        ctx = _ctx(
            fake_channel=fake_channel,
            classifier=_bug_classifier(has_repro=True),
            handle=_sandbox_handle(),
        )

        await maybe_run_triage(ctx)

        assert len(fake_channel.replies) == 1
        assert len(fake_channel.comment_updates) == 1
        final_body = fake_channel.comment_updates[0].message

        # AGENT_FAILED template — spec §5.3
        assert ":robot:" in final_body
        assert "encountered an error" in final_body
        # Exception class name must NOT appear in the user-facing comment
        assert "RuntimeError" not in final_body

    async def test_agent_failure_does_not_propagate_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Agent raising must never propagate out of maybe_run_triage."""
        fake_channel = FakeChannelAdapter()

        async def _fake_generate(_preflight_ctx: PreflightContext) -> ReproOutcome:
            raise ValueError("unexpected internal error")

        monkeypatch.setattr(
            "openbot.application.use_cases.triage._generate_repro_outcome",
            _fake_generate,
        )

        ctx = _ctx(
            fake_channel=fake_channel,
            classifier=_bug_classifier(has_repro=True),
            handle=_sandbox_handle(),
        )

        # Must not raise — background tasks must stay silent
        await maybe_run_triage(ctx)

        # Comment was still posted (failure path still writes the update)
        assert len(fake_channel.comment_updates) == 1

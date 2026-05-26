"""Triage reproduce branch — slice R4 integration matrix.

Pins the spec §6.3 truth-table for the reproduce decision inside
``maybe_run_triage``. Each row asserts the *visible side effects* the
spec promises:

  - which sticky comment body lands (thinking → ack / status template),
  - whether ``_generate_repro_outcome`` was called at all,
  - what string the audit row carries (``ack_only`` vs
    ``reproduce:<status>[:<suffix>]``),
  - what happens on degraded paths (placeholder POST failure, PATCH
    failure → fallback reply, cancellation).

The audit assertion is done by spying on the lifecycle's
``_write_phase`` so we see the actual outcome value the workflow
landed on, without standing up a real database session.

The 9th test (``test_predicate_matches_policy``) formalises the
"predicate ≡ policy" invariant from spec §R1.4 — any drift between
``_should_run_reproduce`` and ``derive_sandbox_policy`` would let the
dispatcher and the use case disagree about whether to provision a
sandbox, and the matrix above would silently regress.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openbot.application.middleware import PreflightContext
from openbot.application.router import Dispatch, SandboxPolicy, derive_task_id
from openbot.application.sandbox_handle import SandboxedHandle
from openbot.application.sandbox_policy import derive_sandbox_policy
from openbot.application.state.cancellation import RunCancelledError
from openbot.application.use_cases import triage as triage_mod
from openbot.dispatcher.classifier import (
    ChatClassifierOutput,
    ClassifierOutput,
    TriageClassifierOutput,
)
from openbot.domain.checkout import CheckoutSpec, CloneStrategy
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.repro import ReproOutcome, ReproStatus
from openbot.domain.workflows import Feature
from openbot.infrastructure.agents.profiles import AgentTimeoutError
from openbot.infrastructure.config_loader import baked_in_defaults
from openbot.testing.fakes import FakeSandbox

# ── Fixtures ────────────────────────────────────────────────────────────────


def _event(**overrides: Any) -> UnifiedEvent:
    defaults: dict[str, Any] = {
        "channel": "github",
        "delivery_id": "del-r4",
        "kind": EventKind.ISSUE_OPENED,
        "repo": "YiAgent/openbot",
        "actor": "alice",
        "actor_type": "User",
        "issue_number": 7,
        "installation_id": 132_536_131,
    }
    defaults.update(overrides)
    return UnifiedEvent(**defaults)


def _classifier(
    *,
    type: str = "bug",
    has_reproduction_info: bool = True,
    looks_like_spam: bool = False,
) -> TriageClassifierOutput:
    """Build a TriageClassifierOutput.

    Default = ``bug`` with ``has_reproduction_info=True`` so callers only
    need to override the axes that vary per test.
    """
    return TriageClassifierOutput(
        type=type,  # type: ignore[arg-type]
        severity_guess="medium",
        has_reproduction_info=has_reproduction_info,
        looks_like_spam=looks_like_spam,
    )


def _handle(sandbox: FakeSandbox | None = None) -> SandboxedHandle:
    """Build a SandboxedHandle. ``sandbox`` defaults to a fresh fake."""
    return SandboxedHandle(
        sandbox=sandbox or FakeSandbox(),
        checkout=CheckoutSpec(
            repo_url="https://github.com/YiAgent/openbot.git",
            ref="deadbeef",
            strategy=CloneStrategy.SHALLOW,
            diff_base=None,
        ),
        token="tok-r4",
    )


def _adapter(**overrides: Any) -> MagicMock:
    """Happy-path adapter; pass kwargs to swap a method (e.g. raise)."""
    a = MagicMock()
    a.reply = AsyncMock(return_value={"id": 4242, "html_url": "https://x"})
    a.update_comment = AsyncMock(return_value={"ok": True, "id": 4242})
    a.get_issue = AsyncMock(
        return_value={
            "title": "AttributeError on foo",
            "body": "Repro: `python -c 'import foo; foo.bar()'`",
            "comments": [],
            "base_sha": "deadbeef",
            "default_branch": "main",
            "clone_url": "https://github.com/YiAgent/openbot.git",
        }
    )
    for name, value in overrides.items():
        setattr(a, name, value)
    return a


def _ctx(
    *,
    adapter: Any,
    event: UnifiedEvent | None = None,
    classifier_output: ClassifierOutput | None = None,
    sandbox_handle: SandboxedHandle | None = None,
) -> PreflightContext:
    """Build a PreflightContext wired for triage reproduce."""
    real_event = event or _event()
    dispatch = Dispatch(
        Feature.TRIAGE,
        triage_mod.maybe_run_triage,
        derive_task_id(real_event),
    )
    return PreflightContext(
        event=real_event,
        dispatch=dispatch,
        config=baked_in_defaults(),
        adapter=adapter,
        session_factory=None,
        redis=None,
        sandbox_handle=sandbox_handle,
        classifier_output=classifier_output,
    )


def _reproduced_outcome() -> ReproOutcome:
    """Canonical REPRODUCED outcome — used by happy-path rows."""
    return ReproOutcome(
        status=ReproStatus.REPRODUCED,
        summary="AttributeError raised as reported",
        command="python -c 'import foo; foo.bar()'",
        exit_code=1,
        output_excerpt="AttributeError: module 'foo' has no attribute 'bar'",
        hypothesis="foo.bar was renamed in 1.2",
        repro_artifact="#!/usr/bin/env bash\npython -c 'import foo; foo.bar()'\n",
        repro_artifact_filename="repro_reproduced.sh",
    )


# ── Audit spy ───────────────────────────────────────────────────────────────


@pytest.fixture
def audit_outcomes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """Capture every audit phase write as ``(phase, outcome)``.

    Patches ``_write_phase`` in the lifecycle module so the spy fires
    regardless of which workflow called it. Returns the list the test
    can inspect after the workflow has returned.
    """
    captured: list[tuple[str, str | None]] = []

    async def spy(ctx: Any, *, workflow: Any, phase: Any, outcome: str | None) -> None:
        captured.append((phase.value, outcome))

    from openbot.application.use_cases import _lifecycle as lifecycle_mod

    monkeypatch.setattr(lifecycle_mod, "_write_phase", spy)
    return captured


# ── Matrix ──────────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_no_classifier_falls_back_to_ack(
    monkeypatch: pytest.MonkeyPatch,
    audit_outcomes: list[tuple[str, str | None]],
) -> None:
    """Classifier failed/absent + no sandbox → triage falls back to ACK.

    The fail-open contract: a missing classifier signal must NOT default
    to running the (expensive) reproduce agent. Without a positive
    bug-with-repro signal AND a provisioned sandbox, the use case
    posts the ACK template and exits.
    """
    fake_generate = AsyncMock()
    monkeypatch.setattr(triage_mod, "_generate_repro_outcome", fake_generate)

    adapter = _adapter()
    await triage_mod.maybe_run_triage(
        _ctx(adapter=adapter, classifier_output=None, sandbox_handle=None)
    )

    # Reproduce agent never ran.
    fake_generate.assert_not_called()
    # Thinking → ACK sticky update.
    assert adapter.reply.await_count == 1
    assert adapter.update_comment.await_count == 1
    final_body = adapter.update_comment.await_args.args[2]
    assert "OpenBot received this issue" in final_body
    assert any(o == "ack_only" for _, o in audit_outcomes)


async def test_non_bug_skips(
    monkeypatch: pytest.MonkeyPatch,
    audit_outcomes: list[tuple[str, str | None]],
) -> None:
    """Classifier returned ``type=question`` → skip reproduce."""
    fake_generate = AsyncMock()
    monkeypatch.setattr(triage_mod, "_generate_repro_outcome", fake_generate)

    adapter = _adapter()
    await triage_mod.maybe_run_triage(
        _ctx(
            adapter=adapter,
            classifier_output=_classifier(type="question"),
            sandbox_handle=None,
        )
    )

    fake_generate.assert_not_called()
    final_body = adapter.update_comment.await_args.args[2]
    assert "OpenBot received this issue" in final_body
    assert any(o == "ack_only" for _, o in audit_outcomes)


async def test_bug_without_repro_info_skips(
    monkeypatch: pytest.MonkeyPatch,
    audit_outcomes: list[tuple[str, str | None]],
) -> None:
    """``type=bug`` but ``has_reproduction_info=False`` → skip reproduce.

    The reproduce loop has nothing to chew on without repro info — the
    responder would burn its 15-call budget on exploration and end with
    ``insufficient_info`` anyway, so we pre-empt that here.
    """
    fake_generate = AsyncMock()
    monkeypatch.setattr(triage_mod, "_generate_repro_outcome", fake_generate)

    adapter = _adapter()
    await triage_mod.maybe_run_triage(
        _ctx(
            adapter=adapter,
            classifier_output=_classifier(has_reproduction_info=False),
            sandbox_handle=None,
        )
    )

    fake_generate.assert_not_called()
    assert any(o == "ack_only" for _, o in audit_outcomes)


async def test_bug_with_repro_info_runs_agent(
    monkeypatch: pytest.MonkeyPatch,
    audit_outcomes: list[tuple[str, str | None]],
) -> None:
    """bug + repro-info + sandbox → run agent, render REPRODUCED template."""
    fake_generate = AsyncMock(return_value=_reproduced_outcome())
    monkeypatch.setattr(triage_mod, "_generate_repro_outcome", fake_generate)

    adapter = _adapter()
    await triage_mod.maybe_run_triage(
        _ctx(
            adapter=adapter,
            classifier_output=_classifier(),
            sandbox_handle=_handle(),
        )
    )

    fake_generate.assert_awaited_once()
    # Thinking placeholder posted first.
    initial_body = adapter.reply.await_args.args[1]
    assert "reproducing this issue" in initial_body
    # Then PATCH'd with the REPRODUCED template.
    final_body = adapter.update_comment.await_args.args[2]
    assert ":white_check_mark:" in final_body
    assert "Reproduced." in final_body
    assert any(o == "reproduce:reproduced" for _, o in audit_outcomes)


async def test_agent_exception_renders_failed_template(
    monkeypatch: pytest.MonkeyPatch,
    audit_outcomes: list[tuple[str, str | None]],
) -> None:
    """Responder raises ``AgentTimeoutError`` → AGENT_FAILED template
    in the comment, class name in the audit row only.

    Spec §5.3: the user-facing body MUST NOT carry the exception class
    name (PII / leakage avoidance). The audit row carries it for ops.
    """

    async def boom(_ctx: PreflightContext) -> ReproOutcome:
        raise AgentTimeoutError("wall_seconds exceeded")

    monkeypatch.setattr(triage_mod, "_generate_repro_outcome", boom)

    adapter = _adapter()
    await triage_mod.maybe_run_triage(
        _ctx(
            adapter=adapter,
            classifier_output=_classifier(),
            sandbox_handle=_handle(),
        )
    )

    final_body = adapter.update_comment.await_args.args[2]
    # Spec §5.3 verbatim body.
    assert "encountered an error and has been" in final_body
    # Class name MUST NOT leak into the user-visible comment.
    assert "AgentTimeoutError" not in final_body
    # …but it MUST land in the audit row for ops.
    assert any(o == "reproduce:agent_failed:AgentTimeoutError" for _, o in audit_outcomes)


async def test_placeholder_post_failure_runs_agent_anyway(
    monkeypatch: pytest.MonkeyPatch,
    audit_outcomes: list[tuple[str, str | None]],
) -> None:
    """Initial ``reply()`` fails → no comment_id, but reproduce still runs.

    The agent's outcome is the more valuable signal; a comment outage
    must not gate the reproduce loop. Audit row carries the ``:no_comment``
    suffix so ops can correlate with the underlying GitHub failure.
    """
    fake_generate = AsyncMock(return_value=_reproduced_outcome())
    monkeypatch.setattr(triage_mod, "_generate_repro_outcome", fake_generate)

    # Both POST attempts fail: initial placeholder + fallback after PATCH no-op.
    adapter = _adapter(reply=AsyncMock(side_effect=RuntimeError("github down")))
    await triage_mod.maybe_run_triage(
        _ctx(
            adapter=adapter,
            classifier_output=_classifier(),
            sandbox_handle=_handle(),
        )
    )

    # Agent ran even though placeholder failed.
    fake_generate.assert_awaited_once()
    # No PATCH attempted (no comment_id to patch).
    adapter.update_comment.assert_not_awaited()
    assert any(o == "reproduce:reproduced:no_comment" for _, o in audit_outcomes)


async def test_update_comment_fails_falls_back_to_reply(
    monkeypatch: pytest.MonkeyPatch,
    audit_outcomes: list[tuple[str, str | None]],
) -> None:
    """PATCH raises → sticky_reply falls back to a fresh reply().

    With ``fallback_on_update_error=True`` the reproduce outcome still
    lands on the issue thread, just as a second comment instead of an
    in-place edit. Audit row carries the unsuffixed outcome because the
    initial placeholder POST succeeded (comment_id was set).
    """
    fake_generate = AsyncMock(return_value=_reproduced_outcome())
    monkeypatch.setattr(triage_mod, "_generate_repro_outcome", fake_generate)

    adapter = _adapter(
        update_comment=AsyncMock(side_effect=RuntimeError("patch 403")),
    )
    await triage_mod.maybe_run_triage(
        _ctx(
            adapter=adapter,
            classifier_output=_classifier(),
            sandbox_handle=_handle(),
        )
    )

    # Initial placeholder + fallback reply.
    assert adapter.reply.await_count == 2
    # PATCH was attempted exactly once and failed.
    adapter.update_comment.assert_awaited_once()
    # The fallback carried the REPRODUCED template body.
    fallback_body = adapter.reply.await_args_list[-1].args[1]
    assert "Reproduced." in fallback_body
    # No ``:no_comment`` suffix — the placeholder POST succeeded, the
    # PATCH failure is a separate degrade and is logged by sticky_reply.
    assert any(o == "reproduce:reproduced" for _, o in audit_outcomes)


async def test_cancellation_propagates_and_writes_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    audit_outcomes: list[tuple[str, str | None]],
) -> None:
    """``RunCancelledError`` propagates through the use case + writes CANCELLED.

    The lifecycle helper special-cases ``asyncio.CancelledError`` (which
    ``RunCancelledError`` subclasses) to emit a CANCELLED row before
    re-raising. The exception then propagates out of ``maybe_run_triage``
    to the worker task — matching the pattern in ``fix.py`` / ``review.py``
    so the worker's cancellation coordinator can observe it.
    """

    async def cancelled(_ctx: PreflightContext) -> ReproOutcome:
        raise RunCancelledError()

    monkeypatch.setattr(triage_mod, "_generate_repro_outcome", cancelled)

    adapter = _adapter()
    with pytest.raises(RunCancelledError):
        await triage_mod.maybe_run_triage(
            _ctx(
                adapter=adapter,
                classifier_output=_classifier(),
                sandbox_handle=_handle(),
            )
        )

    # Thinking placeholder posted; no final update attempted (cancel
    # arrived between the agent call and the sticky.update).
    assert adapter.reply.await_count == 1
    adapter.update_comment.assert_not_awaited()
    # CANCELLED row landed.
    assert any(
        phase == "cancelled" and outcome == "RunCancelledError" for phase, outcome in audit_outcomes
    )


# ── Predicate ≡ policy invariant ────────────────────────────────────────────


@pytest.mark.parametrize(
    "classifier_output",
    [
        None,
        _classifier(type="bug", has_reproduction_info=True),
        _classifier(type="bug", has_reproduction_info=False),
        _classifier(type="bug", has_reproduction_info=True, looks_like_spam=True),
        _classifier(type="feature", has_reproduction_info=True),
        _classifier(type="question", has_reproduction_info=False),
        _classifier(type="spam", has_reproduction_info=False),
        _classifier(type="other", has_reproduction_info=True),
        # A wrong-shape classifier (chat output bleeding into triage)
        # must NOT trip the predicate — defensive default in the policy
        # fn is REQUIRED, so the predicate stays True.
        ChatClassifierOutput(intent="readonly_qa", needs_clarification=False, scope_hint=None),
    ],
)
def test_predicate_matches_policy(classifier_output: ClassifierOutput | None) -> None:
    """``_should_run_reproduce`` MUST agree with ``derive_sandbox_policy``.

    The dispatcher uses the policy fn to decide whether to provision a
    sandbox; the use case uses the predicate to decide whether to call
    the responder. Any drift = dispatcher provisions a sandbox the use
    case won't use (waste) OR use case asks for a sandbox the dispatcher
    didn't provision (crash).

    The invariant holds when ``sandbox_handle`` is set — the predicate
    incorporates handle presence as a second gate (handled by the
    integration matrix above), but the *policy slice* of the predicate
    must track the policy fn exactly.
    """
    adapter = _adapter()
    ctx = _ctx(
        adapter=adapter,
        classifier_output=classifier_output,
        sandbox_handle=_handle(),  # hold sandbox constant; test the policy axis only
    )

    expected = (
        derive_sandbox_policy(
            static=SandboxPolicy.REQUIRED,
            classifier_output=classifier_output,
            feature=Feature.TRIAGE,
        )
        is SandboxPolicy.REQUIRED
    )
    assert triage_mod._should_run_reproduce(ctx) is expected


def test_predicate_false_without_sandbox_handle() -> None:
    """No ``sandbox_handle`` → predicate False even on a bug-with-repro signal.

    The dispatcher signals "sandbox provisioning failed" by leaving the
    handle ``None``; calling the responder anyway would raise
    ``AgentSandboxRequiredError``. The use case treats the missing
    handle as the fall-back-to-ACK path.
    """
    adapter = _adapter()
    ctx = _ctx(
        adapter=adapter,
        classifier_output=_classifier(),  # bug + repro → policy says REQUIRED
        sandbox_handle=None,
    )
    assert triage_mod._should_run_reproduce(ctx) is False


# ── Untouched-existing-flow guards ──────────────────────────────────────────


async def test_non_issue_opened_event_is_still_no_op(
    audit_outcomes: list[tuple[str, str | None]],
) -> None:
    """Reproduce branch must not regress the existing ``kind`` guard."""
    adapter = _adapter()
    await triage_mod.maybe_run_triage(
        _ctx(
            adapter=adapter,
            event=_event(kind=EventKind.ISSUE_COMMENT_CREATED),
            classifier_output=_classifier(),
            sandbox_handle=_handle(),
        )
    )
    adapter.reply.assert_not_awaited()
    adapter.update_comment.assert_not_awaited()
    assert audit_outcomes == []  # no lifecycle started

    # Silence the asynccontextmanager import — kept for completeness in
    # case future tests want to wrap the lifecycle themselves.
    _ = asynccontextmanager

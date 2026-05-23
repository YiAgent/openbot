"""Use case tests for ``maybe_run_fix`` (slice C end-to-end).

Part 3 of the unified-sandbox-entry plan moved provisioning (factory →
clone → installation token) up into ``dispatcher._run_with_sandbox``,
so this suite no longer covers those branches — the dispatcher's own
test (``tests/application/test_dispatcher.py``) does. The fix handler
now receives a pre-built ``SandboxedHandle`` on ``ctx.sandbox_handle``
and either consumes it or posts the ``_NO_SANDBOX`` degrade reply.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openbot.application.sandbox_handle import SandboxedHandle
from openbot.application.use_cases import fix as fix_module
from openbot.domain.checkout import CheckoutSpec, CloneStrategy
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.fix import FixAttempt, FixOutcome
from tests._fakes.sandbox import FakeSandboxLifecycle


def _event(**overrides: Any) -> UnifiedEvent:
    defaults: dict[str, Any] = {
        "channel": "github",
        "delivery_id": "del-1",
        "kind": EventKind.ISSUE_ASSIGNED,
        "repo": "o/r",
        "actor": "alice",
        "installation_id": 999,
        "issue_number": 7,
        "pr_number": None,
    }
    defaults.update(overrides)
    return UnifiedEvent(**defaults)


def _attempt(**overrides: Any) -> FixAttempt:
    defaults: dict[str, Any] = {
        "summary": "fix off-by-one",
        "files_changed": ("src/api/list.py",),
        "tests_passed": True,
        "test_command": "pytest -q",
        "test_output": "3 passed",
        "diff": "diff --git a/src/api/list.py b/src/api/list.py\n",
    }
    defaults.update(overrides)
    return FixAttempt(**defaults)


def _issue() -> dict[str, Any]:
    return {
        "title": "Off-by-one",
        "body": "details",
        "base_sha": "abc1234567",
        "default_branch": "main",
        "clone_url": "https://github.com/o/r.git",
    }


def _checkout(**overrides: Any) -> CheckoutSpec:
    """Build a CheckoutSpec matching the canonical ``_issue()`` fixture
    so the branch-naming + commit-and-push assertions still line up."""
    defaults: dict[str, Any] = {
        "repo_url": "https://github.com/o/r.git",
        "ref": "abc1234567",
        "strategy": CloneStrategy.SHALLOW,
        "diff_base": None,
    }
    defaults.update(overrides)
    return CheckoutSpec(**defaults)


def _handle(
    *,
    sandbox: FakeSandboxLifecycle | None = None,
    checkout: CheckoutSpec | None = None,
    token: str = "tok123",
) -> SandboxedHandle:
    """The dispatcher builds this; tests inject it directly into ctx.

    Keeps the three correlated values together — see
    ``openbot.application.sandbox_handle`` docstring.
    """
    return SandboxedHandle(
        sandbox=sandbox or FakeSandboxLifecycle(),
        checkout=checkout or _checkout(),
        token=token,
    )


def _adapter(**method_overrides: Any) -> MagicMock:
    """Happy-path adapter mock; pass kwargs to override a method
    (e.g. ``get_issue=AsyncMock(side_effect=RuntimeError())``).

    ``get_installation_token`` returns a fresh token string matching the
    handle's default token.  The fix handler refreshes the token
    immediately before commit_and_push to guard against 1-hour token
    expiry during long agent runs.
    """
    a = MagicMock()
    a.get_issue = AsyncMock(return_value=_issue())
    a.get_installation_token = AsyncMock(return_value="tok123")
    a.create_branch = AsyncMock()
    a.open_pull_request = AsyncMock(
        return_value={"html_url": "https://github.com/o/r/pull/9"},
    )
    a.reply = AsyncMock(return_value={"id": 1})
    for name, value in method_overrides.items():
        setattr(a, name, value)
    return a


def _ctx(
    *,
    event: UnifiedEvent | None = None,
    adapter: Any,
    sandbox_handle: SandboxedHandle | None,
    run_id: str | None = None,
    agent_checkpointer: Any = None,
) -> Any:
    from openbot.application.middleware import PreflightContext
    from openbot.application.router import Dispatch, derive_task_id
    from openbot.application.use_cases.fix import maybe_run_fix
    from openbot.domain.workflows import Feature
    from openbot.infrastructure.config_loader import baked_in_defaults

    real_event = event or _event()
    return PreflightContext(
        event=real_event,
        dispatch=Dispatch(Feature.FIX, maybe_run_fix, derive_task_id(real_event), run_id=run_id),
        config=baked_in_defaults(),
        adapter=adapter,
        session_factory=None,
        redis=None,
        sandbox_handle=sandbox_handle,
        agent_checkpointer=agent_checkpointer,
    )


# ---------- Skip paths ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("field_to_drop", ["issue_number", "installation_id"])
async def test_skips_when_required_event_field_missing(field_to_drop: str):
    adapter = _adapter()
    ctx = _ctx(
        event=_event(**{field_to_drop: None}),
        adapter=adapter,
        sandbox_handle=_handle(),
    )

    await fix_module.maybe_run_fix(ctx)

    adapter.reply.assert_not_called()
    adapter.get_issue.assert_not_called()


@pytest.mark.asyncio
async def test_fix_degrades_when_sandbox_handle_none():
    """Handler given ``ctx.sandbox_handle is None`` posts the
    ``_NO_SANDBOX`` template and never touches GitHub.

    This is the unified-entry degrade contract — the dispatcher signals
    "no sandbox available" by passing None, and the handler chooses a
    workflow-appropriate reply rather than the dispatcher trying to
    guess per-feature templates.
    """
    adapter = _adapter()
    ctx = _ctx(adapter=adapter, sandbox_handle=None)

    await fix_module.maybe_run_fix(ctx)

    posted = adapter.reply.call_args.args[1].lower()
    assert "sandbox is not configured" in posted
    # No GitHub work proceeds — `get_issue` is the first call inside
    # the lifecycle block, and the early return happens above it.
    adapter.get_issue.assert_not_called()


# ---------- Happy path ----------


@pytest.mark.asyncio
async def test_fix_uses_sandbox_handle_from_context(monkeypatch):
    """Handler consumes the pre-provisioned handle: never opens its own
    sandbox, never fetches the installation token, never calls clone.

    The dispatcher has already done all three, so a second clone would
    be wasted work *and* would race the dispatcher's checkout.
    """
    sandbox = FakeSandboxLifecycle()
    adapter = _adapter()

    async def fake_generate(*, sandbox, event, adapter, issue, run_id=None, checkpointer=None):
        return FixOutcome(attempt=_attempt(tests_passed=True))

    monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)

    ctx = _ctx(adapter=adapter, sandbox_handle=_handle(sandbox=sandbox))
    await fix_module.maybe_run_fix(ctx)

    # The handler did NOT re-clone — provisioning is the dispatcher's job.
    assert sandbox.cloned == []
    # The handler refreshes the installation token before commit_and_push
    # to guard against 1-hour token expiry during long agent runs.
    assert adapter.get_installation_token.called

    # Branch name pattern + short SHA come from the handle's checkout.
    branch_ref = adapter.create_branch.call_args.args[1]
    assert branch_ref.startswith("openbot/fix-issue-7-")
    assert "abc1234" in branch_ref

    # PR body closes the issue; head/base wired from issue dict.
    pr_kwargs = adapter.open_pull_request.call_args.kwargs
    assert pr_kwargs["base"] == "main"
    assert pr_kwargs["head"].startswith("openbot/fix-issue-7-")
    assert "Closes #7" in pr_kwargs["body"]
    # commit_and_push receives the short branch_ref + the handle's token.
    assert sandbox.pushed
    assert sandbox.pushed[0][0].startswith("openbot/fix-issue-7-")
    assert sandbox.pushed[0][2] == "tok123"

    # Final comment carries the PR URL.
    assert "https://github.com/o/r/pull/9" in adapter.reply.call_args.args[1]


@pytest.mark.asyncio
async def test_audit_lifecycle_records_pr_url_on_success(monkeypatch):
    sandbox = FakeSandboxLifecycle()
    adapter = _adapter()
    captured: list[str] = []

    @asynccontextmanager
    async def fake_audit_lifecycle(ctx, *, workflow):
        rec = type("R", (), {"outcome": ""})()
        try:
            yield rec
        finally:
            captured.append(rec.outcome)

    monkeypatch.setattr(fix_module, "audit_lifecycle", fake_audit_lifecycle)

    async def fake_generate(*, sandbox, event, adapter, issue, run_id=None, checkpointer=None):
        return FixOutcome(attempt=_attempt(tests_passed=True))

    monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)

    ctx = _ctx(adapter=adapter, sandbox_handle=_handle(sandbox=sandbox))
    await fix_module.maybe_run_fix(ctx)

    assert captured == ["pr_opened:https://github.com/o/r/pull/9"]


# ---------- Tests-failed path (carries truncation check) ----------


@pytest.mark.asyncio
async def test_comments_with_truncated_output_when_tests_failed(monkeypatch):
    sandbox = FakeSandboxLifecycle()
    adapter = _adapter()
    huge = "X" * 50_000

    async def fake_generate(*, sandbox, event, adapter, issue, run_id=None, checkpointer=None):
        return FixOutcome(
            attempt=_attempt(tests_passed=False, test_output=huge),
        )

    monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)

    ctx = _ctx(adapter=adapter, sandbox_handle=_handle(sandbox=sandbox))
    await fix_module.maybe_run_fix(ctx)

    adapter.create_branch.assert_not_called()
    adapter.open_pull_request.assert_not_called()
    posted = adapter.reply.call_args.args[1]
    assert "tests did not pass" in posted.lower()
    assert "truncated" in posted.lower()
    assert len(posted) < 10_000  # well under GitHub's 65k cap


# ---------- Failure paths (parametrized over the stage that explodes) ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "expected_phrase", "expect_pr_attempt"),
    [
        ("get_issue", "could not read", False),
        ("agent", "agent failed", False),
        ("create_branch", "branch", False),
        ("push", "push", False),
        ("open_pull_request", "could not open", True),
    ],
)
async def test_failure_in_stage_yields_tailored_comment(
    monkeypatch,
    stage: str,
    expected_phrase: str,
    expect_pr_attempt: bool,
):
    """One parametrized case per stage. Setup is shared — only the
    failing dependency varies, plus the user-visible comment phrase.

    The ``clone`` stage isn't in this matrix anymore: that's the
    dispatcher's responsibility (it degrades to ``sandbox_handle =
    None`` on a clone failure, which the ``_NO_SANDBOX`` branch above
    covers).
    """
    sandbox: FakeSandboxLifecycle = FakeSandboxLifecycle()
    adapter_overrides: dict[str, Any] = {}

    if stage == "get_issue":
        adapter_overrides["get_issue"] = AsyncMock(side_effect=RuntimeError("404"))
    elif stage == "agent":

        async def fake_generate(*, sandbox, event, adapter, issue, run_id=None, checkpointer=None):
            raise RuntimeError("agent imploded")

        monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)
    elif stage == "create_branch":
        adapter_overrides["create_branch"] = AsyncMock(
            side_effect=RuntimeError("ref exists"),
        )
    elif stage == "push":

        class PushFail(FakeSandboxLifecycle):
            async def commit_and_push(self, *args, **kwargs) -> None:
                raise RuntimeError("auth failed")

        sandbox = PushFail()
    elif stage == "open_pull_request":
        adapter_overrides["open_pull_request"] = AsyncMock(
            side_effect=RuntimeError("validation_failed"),
        )

    if stage != "agent":

        async def fake_generate(*, sandbox, event, adapter, issue, run_id=None, checkpointer=None):
            return FixOutcome(attempt=_attempt(tests_passed=True))

        monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)

    adapter = _adapter(**adapter_overrides)
    ctx = _ctx(adapter=adapter, sandbox_handle=_handle(sandbox=sandbox))
    await fix_module.maybe_run_fix(ctx)

    if not expect_pr_attempt:
        adapter.open_pull_request.assert_not_called()

    assert expected_phrase in adapter.reply.call_args.args[1].lower()


# ---------- Checkpointer + cancellation ----------


@pytest.mark.asyncio
async def test_fix_passes_checkpointer_and_run_id_to_responder(monkeypatch) -> None:
    """maybe_run_fix must forward ctx.agent_checkpointer + ctx.dispatch.run_id
    to _generate_fix_outcome."""
    from langgraph.checkpoint.memory import MemorySaver

    captured: dict[str, Any] = {}

    async def fake_generate(
        *, sandbox, event, adapter, issue, run_id=None, checkpointer=None
    ) -> FixOutcome:
        captured["run_id"] = run_id
        captured["checkpointer"] = checkpointer
        return FixOutcome(attempt=_attempt(tests_passed=True))

    monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)

    saver = MemorySaver()
    ctx = _ctx(
        adapter=_adapter(),
        sandbox_handle=_handle(),
        run_id="run-fix-1",
        agent_checkpointer=saver,
    )
    await fix_module.maybe_run_fix(ctx)

    assert captured["run_id"] == "run-fix-1"
    assert captured["checkpointer"] is saver


@pytest.mark.asyncio
async def test_fix_cancellation_checkpoint_fires_before_agent(monkeypatch) -> None:
    """If cancellation is signalled before the agent call, RunCancelledError propagates.

    RunCancelledError inherits from asyncio.CancelledError → BaseException, so
    audit_lifecycle's ``except Exception`` guard does NOT intercept it.
    """
    from openbot.application.state.cancellation import RunCancelledError

    async def _always_cancelled(redis: Any, run_id: str) -> None:
        raise RunCancelledError()

    monkeypatch.setattr(fix_module, "checkpoint", _always_cancelled)

    ctx = _ctx(
        adapter=_adapter(),
        sandbox_handle=_handle(),
        run_id="run-cancel-test",
    )
    with pytest.raises(RunCancelledError):
        await fix_module.maybe_run_fix(ctx)

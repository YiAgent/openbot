"""Use case tests for ``maybe_run_fix`` (slice C end-to-end)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openbot.application.use_cases import fix as fix_module
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.fix import FixAttempt, FixOutcome


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


@dataclass
class _FakeSandbox:
    """In-test stand-in for ``SandboxPort``. Records each call as a
    ``(repo_url, ref, token)`` or ``(branch_ref, message, token)`` tuple
    so wiring tests can assert on the exact contract the use case must
    honour against the real ``DaytonaSandboxAdapter``.
    """

    workspace: str = "/workspace/repo"
    cloned: list[tuple[str, str, str]] = field(default_factory=list)
    pushed: list[tuple[str, str, str]] = field(default_factory=list)
    closed: bool = False

    async def clone(self, *, repo_url: str, ref: str, token: str) -> None:
        self.cloned.append((repo_url, ref, token))

    async def commit_and_push(self, *, branch_ref: str, message: str, token: str) -> None:
        self.pushed.append((branch_ref, message, token))

    async def close(self) -> None:
        self.closed = True


@asynccontextmanager
async def _sandbox_cm(sandbox: _FakeSandbox):
    try:
        yield sandbox
    finally:
        await sandbox.close()


def _adapter(**method_overrides: Any) -> MagicMock:
    """Happy-path adapter mock; pass kwargs to override a method
    (e.g. ``get_issue=AsyncMock(side_effect=RuntimeError())``).
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


def _ctx(*, event: UnifiedEvent | None = None, adapter: Any, sandbox_factory: Any) -> Any:
    from openbot.application.middleware import PreflightContext
    from openbot.application.router import Dispatch, derive_task_id
    from openbot.application.use_cases.fix import maybe_run_fix
    from openbot.domain.workflows import Feature
    from openbot.infrastructure.config_loader import baked_in_defaults

    real_event = event or _event()
    return PreflightContext(
        event=real_event,
        dispatch=Dispatch(Feature.FIX, maybe_run_fix, derive_task_id(real_event)),
        config=baked_in_defaults(),
        adapter=adapter,
        session_factory=None,
        redis=None,
        sandbox_factory=sandbox_factory,
    )


# ---------- Skip paths ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("field_to_drop", ["issue_number", "installation_id"])
async def test_skips_when_required_event_field_missing(field_to_drop: str):
    adapter = _adapter()
    ctx = _ctx(
        event=_event(**{field_to_drop: None}),
        adapter=adapter,
        sandbox_factory=lambda: _sandbox_cm(_FakeSandbox()),
    )

    await fix_module.maybe_run_fix(ctx)

    adapter.reply.assert_not_called()
    adapter.get_issue.assert_not_called()


@pytest.mark.asyncio
async def test_comments_when_sandbox_factory_missing():
    adapter = _adapter()
    ctx = _ctx(adapter=adapter, sandbox_factory=None)

    await fix_module.maybe_run_fix(ctx)

    assert "sandbox is not configured" in adapter.reply.call_args.args[1].lower()


# ---------- Happy path ----------


@pytest.mark.asyncio
async def test_fetches_issue_clones_and_opens_pr(monkeypatch):
    sandbox = _FakeSandbox()
    adapter = _adapter()

    async def fake_generate(*, sandbox, event, adapter, issue):
        return FixOutcome(attempt=_attempt(tests_passed=True))

    monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)

    ctx = _ctx(adapter=adapter, sandbox_factory=lambda: _sandbox_cm(sandbox))
    await fix_module.maybe_run_fix(ctx)

    # Use case passes raw clone_url + ref + token through to the port —
    # token injection is the adapter's job (see SandboxPort docstring).
    assert sandbox.cloned == [
        ("https://github.com/o/r.git", "abc1234567", "tok123"),
    ]
    assert sandbox.closed is True

    # Branch name pattern + short SHA.
    branch_ref = adapter.create_branch.call_args.args[1]
    assert branch_ref.startswith("refs/heads/openbot/fix-issue-7-")
    assert "abc1234" in branch_ref

    # PR body closes the issue; head/base wired from issue dict.
    pr_kwargs = adapter.open_pull_request.call_args.kwargs
    assert pr_kwargs["base"] == "main"
    assert pr_kwargs["head"].startswith("openbot/fix-issue-7-")
    assert "Closes #7" in pr_kwargs["body"]
    # commit_and_push receives the *short* branch_ref + the raw token.
    assert sandbox.pushed
    assert sandbox.pushed[0][0].startswith("openbot/fix-issue-7-")
    assert sandbox.pushed[0][2] == "tok123"

    # Final comment carries the PR URL.
    assert "https://github.com/o/r/pull/9" in adapter.reply.call_args.args[1]


@pytest.mark.asyncio
async def test_audit_lifecycle_records_pr_url_on_success(monkeypatch):
    sandbox = _FakeSandbox()
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

    async def fake_generate(*, sandbox, event, adapter, issue):
        return FixOutcome(attempt=_attempt(tests_passed=True))

    monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)

    ctx = _ctx(adapter=adapter, sandbox_factory=lambda: _sandbox_cm(sandbox))
    await fix_module.maybe_run_fix(ctx)

    assert captured == ["pr_opened:https://github.com/o/r/pull/9"]


# ---------- Tests-failed path (carries truncation check) ----------


@pytest.mark.asyncio
async def test_comments_with_truncated_output_when_tests_failed(monkeypatch):
    sandbox = _FakeSandbox()
    adapter = _adapter()
    huge = "X" * 50_000

    async def fake_generate(*, sandbox, event, adapter, issue):
        return FixOutcome(
            attempt=_attempt(tests_passed=False, test_output=huge),
        )

    monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)

    ctx = _ctx(adapter=adapter, sandbox_factory=lambda: _sandbox_cm(sandbox))
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
        ("clone", "clone", False),
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
    """
    sandbox: _FakeSandbox = _FakeSandbox()
    adapter_overrides: dict[str, Any] = {}

    if stage == "get_issue":
        adapter_overrides["get_issue"] = AsyncMock(side_effect=RuntimeError("404"))
    elif stage == "clone":

        class ExplodingClone(_FakeSandbox):
            async def clone(self, *args, **kwargs) -> None:
                raise RuntimeError("clone refused")

        sandbox = ExplodingClone()
    elif stage == "agent":

        async def fake_generate(*, sandbox, event, adapter, issue):
            raise RuntimeError("agent imploded")

        monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)
    elif stage == "create_branch":
        adapter_overrides["create_branch"] = AsyncMock(
            side_effect=RuntimeError("ref exists"),
        )
    elif stage == "push":

        class PushFail(_FakeSandbox):
            async def commit_and_push(self, *args, **kwargs) -> None:
                raise RuntimeError("auth failed")

        sandbox = PushFail()
    elif stage == "open_pull_request":
        adapter_overrides["open_pull_request"] = AsyncMock(
            side_effect=RuntimeError("validation_failed"),
        )

    if stage != "agent":

        async def fake_generate(*, sandbox, event, adapter, issue):
            return FixOutcome(attempt=_attempt(tests_passed=True))

        monkeypatch.setattr(fix_module, "_generate_fix_outcome", fake_generate)

    adapter = _adapter(**adapter_overrides)
    ctx = _ctx(adapter=adapter, sandbox_factory=lambda: _sandbox_cm(sandbox))
    await fix_module.maybe_run_fix(ctx)

    # Sandbox CM cleanup ran unless we never entered it (get_issue path).
    if stage == "get_issue":
        assert sandbox.cloned == []
    else:
        assert sandbox.closed is True

    if not expect_pr_attempt:
        adapter.open_pull_request.assert_not_called()

    assert expected_phrase in adapter.reply.call_args.args[1].lower()

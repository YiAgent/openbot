"""DeepAgentsReproResponder + ReproProfile — wiring and contract tests.

Mirrors ``test_deepagents_fix.py``; the security-critical row is
``test_build_tools_returns_read_only_subset`` which pins the tool
whitelist against the **real** ``_fix_tools`` names so a stealth
rename in ``_fix_tools.make_fix_tools`` can't quietly widen the
reproduce-agent's surface.

The ``ReproProfile`` lives under ``Feature.TRIAGE`` (not a new
feature) because reproduce is a sub-step *inside* the triage workflow
— the audit row's ``workflow`` column is still ``triage``; the
``outcome`` column carries ``reproduce:<status>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.repro import ReproOutcome, ReproStatus
from openbot.domain.workflows import Feature
from openbot.infrastructure.agents.profiles import (
    AgentRequest,
    AgentSandboxRequiredError,
    AgentStructuredOutputError,
    SandboxRequirement,
)

# ── Fixtures / fakes ─────────────────────────────────────────────────────────


def _event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="d-repro",
        kind=EventKind.ISSUE_OPENED,
        repo="o/r",
        actor="alice",
        issue_number=7,
        installation_id=1,
    )


@dataclass
class _StubAdapter:
    """Adapter is unused by the responder body (the use case handles
    GitHub I/O), but the responder accepts one for signature symmetry
    with the other ``deepagents_*`` modules."""


@dataclass
class _StubSandbox:
    """The reproduce agent only needs an object with the read-only
    SandboxPort surface; ``make_repro_tools`` calls ``read_file`` /
    ``list_files`` / ``run`` on it. The fake agent we monkeypatch
    never invokes the tools, so the stub stays empty."""


def _valid_outcome_dict(status: str = "reproduced") -> dict[str, Any]:
    """Shape matching ``ReproOutcomeSchema`` for a REPRODUCED outcome.

    Each test that needs a different status overrides only the fields
    that change — keeps the asserts honest about which field matters.
    """

    return {
        "status": status,
        "summary": "IndexError reproduced on empty list",
        "command": "pytest -q tests/test_list.py",
        "exit_code": 1,
        "output_excerpt": "IndexError: list index out of range",
        "hypothesis": "pop() called without length guard at api/list.py:42",
        "repro_artifact": "#!/usr/bin/env bash\npytest -q tests/test_list.py\n",
        "repro_artifact_filename": "repro_reproduced.sh",
    }


def _fake_agent_result(outcome: dict[str, Any] | None = None) -> dict[str, Any]:
    """Shape returned by ``create_deep_agent(...).ainvoke(...)`` when
    ``response_format=ReproOutcomeSchema`` is set."""

    return {
        "messages": [],
        "structured_response": outcome if outcome is not None else _valid_outcome_dict(),
    }


# ── Profile attributes ──────────────────────────────────────────────────────


def test_repro_profile_static_attributes() -> None:
    """ReproProfile pins the same shape across runs — invariants the
    use case and runtime both depend on."""
    from openbot.infrastructure.agents.deepagents_repro import ReproProfile

    profile = ReproProfile()
    assert profile.feature is Feature.TRIAGE
    assert profile.agent_name == "repro"
    assert profile.sandbox_requirement is SandboxRequirement.REQUIRED
    assert profile.checkpoint_enabled is True


def test_repro_profile_limits_match_spec() -> None:
    """Limits are a security boundary, not a perf knob — spec §3.4 fixes
    the exact numbers. Any change here is a deliberate spec change."""
    from openbot.infrastructure.agents.deepagents_repro import ReproProfile

    limits = ReproProfile().limits
    assert limits.recursion_limit == 40
    assert limits.model_call_limit == 15
    assert limits.tool_call_limit == 30
    assert limits.wall_seconds == 180
    assert limits.max_output_tokens == 4000
    assert limits.thinking_budget_tokens == 0


# ── Prompt content ──────────────────────────────────────────────────────────


def test_system_prompt_contains_insufficient_info_early_exit() -> None:
    """The early-exit instruction is the only path that prevents the
    agent from burning its full budget on a malformed issue. Pin it.

    Spec §6.2 contract: ``system_prompt(request)`` must contain the
    ``INSUFFICIENT_INFO`` early-exit instruction. Tool surface is
    pinned separately by ``test_build_tools_returns_read_only_subset``
    — that's the real security boundary, not prompt content."""
    from openbot.infrastructure.agents.deepagents_repro import ReproProfile

    request = AgentRequest(event=_event(), input={"issue_title": "t", "issue_body": "b"})
    prompt = ReproProfile().system_prompt(request).lower()
    assert "insufficient_info" in prompt or "insufficient info" in prompt
    # Pin the spirit of early-exit: the prompt must talk about exiting
    # early / immediately when info is missing, not just mention the
    # enum value in passing.
    assert "early" in prompt or "immediately" in prompt


def test_user_message_contains_issue_context() -> None:
    from openbot.infrastructure.agents.deepagents_repro import ReproProfile

    request = AgentRequest(
        event=_event(),
        input={
            "issue_title": "IndexError on empty list",
            "issue_body": "Steps: `from api.list import pop; pop([])`",
            "base_sha": "abc1234",
        },
    )
    msg = ReproProfile().user_message(request)
    assert "IndexError on empty list" in msg
    assert "pop([])" in msg
    assert "abc1234" in msg
    assert "o/r" in msg
    assert "#7" in msg


# ── Tool whitelist contract (security-critical) ─────────────────────────────


def test_build_tools_returns_read_only_subset() -> None:
    """**The** security-critical test for this profile.

    The whitelist is a hard ceiling — the reproduce agent must never
    gain access to mutating or repo-leaking surfaces. Asserting against
    the *real* fix-tool names (not invented strings like ``apply_patch``)
    means this test would catch a stealth rename in ``_fix_tools`` that
    accidentally widened the reproduce surface.
    """
    from openbot.infrastructure.agents.deepagents_repro import ReproProfile

    request = AgentRequest(event=_event(), input={}, sandbox=_StubSandbox())  # type: ignore[arg-type]
    tools = ReproProfile().build_tools(request)
    names = {t.name for t in tools}
    assert names <= {"read_file", "list_files", "run_command"}, (
        f"reproduce surface must stay read-only; got: {sorted(names)}"
    )
    assert names.isdisjoint({"write_file", "git_diff", "search_files"}), (
        f"forbidden fix-tool name leaked into reproduce surface: {sorted(names)}"
    )


def test_build_tools_raises_when_sandbox_missing() -> None:
    """sandbox_requirement=REQUIRED is enforced at the runtime layer,
    but the profile guards against a direct caller passing
    ``request.sandbox=None`` outside the runtime path."""
    from openbot.infrastructure.agents.deepagents_repro import ReproProfile

    request = AgentRequest(event=_event(), input={}, sandbox=None)
    with pytest.raises(AgentSandboxRequiredError):
        ReproProfile().build_tools(request)


# ── parse_result ────────────────────────────────────────────────────────────


def test_parse_result_returns_repro_outcome() -> None:
    from openbot.infrastructure.agents.deepagents_repro import ReproProfile

    outcome = ReproProfile().parse_result({"structured_response": _valid_outcome_dict()})
    assert isinstance(outcome, ReproOutcome)
    assert outcome.status is ReproStatus.REPRODUCED
    assert outcome.command == "pytest -q tests/test_list.py"
    assert outcome.exit_code == 1
    assert outcome.repro_artifact_filename == "repro_reproduced.sh"


def test_parse_result_missing_structured_response_raises() -> None:
    from openbot.infrastructure.agents.deepagents_repro import ReproProfile

    with pytest.raises(
        AgentStructuredOutputError,
        match="deepagents_repro_result_missing_structured_response",
    ):
        ReproProfile().parse_result({"messages": []})  # no structured_response key


# ── Responder delegation ────────────────────────────────────────────────────


async def test_repro_responder_delegates_to_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compatibility wrapper must delegate to BaseDeepAgentRuntime.run.

    Mirrors ``test_fix_responder_delegates_to_runtime`` — the responder
    is a thin shim and its only behaviour is "build AgentRequest, hand
    off to runtime, return whatever the runtime returned".
    """
    from openbot.infrastructure.agents.deepagents_repro import (
        DeepAgentsReproResponder,
        ReproProfile,
    )
    from openbot.infrastructure.agents.runtime import BaseDeepAgentRuntime

    run_calls: list[Any] = []

    async def fake_run(self: Any, profile: Any, request: Any) -> ReproOutcome:
        run_calls.append((profile, request))
        return ReproOutcome(
            status=ReproStatus.REPRODUCED,
            summary="delegated",
            command="pytest -q",
            exit_code=1,
            output_excerpt="…",
            hypothesis="…",
            repro_artifact="#!/bin/sh\npytest -q\n",
            repro_artifact_filename="repro_reproduced.sh",
        )

    monkeypatch.setattr(BaseDeepAgentRuntime, "run", fake_run)

    outcome = await DeepAgentsReproResponder().reproduce_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={"title": "Bug", "body": "It's broken", "base_sha": "abc123"},
    )

    assert len(run_calls) == 1
    assert isinstance(run_calls[0][0], ReproProfile)
    assert outcome.status is ReproStatus.REPRODUCED
    assert outcome.summary == "delegated"


async def test_repro_responder_passes_issue_context_to_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``issue`` dict keys flow into ``AgentRequest.input`` so the
    profile's ``user_message`` can read them. Mirrors fix's input shape."""
    import openbot.infrastructure.agents.runtime as runtime_mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            captured["payload"] = payload
            return _fake_agent_result()

    monkeypatch.setattr(runtime_mod, "create_deep_agent", lambda **_: FakeAgent())

    from openbot.infrastructure.agents.deepagents_repro import DeepAgentsReproResponder

    await DeepAgentsReproResponder().reproduce_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={
            "title": "IndexError on empty list",
            "body": "pop([]) crashes",
            "base_sha": "abc1234",
        },
    )

    user_msg = captured["payload"]["messages"][0]["content"]
    assert "IndexError on empty list" in user_msg
    assert "pop([])" in user_msg
    assert "abc1234" in user_msg


async def test_repro_responder_raises_when_issue_number_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No issue number → no usable prompt; fail loudly before burning
    a model call rendering ``#None``."""
    import openbot.infrastructure.agents.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "create_deep_agent", lambda **_: object())

    from openbot.infrastructure.agents.deepagents_repro import DeepAgentsReproResponder

    event = UnifiedEvent(
        channel="github",
        delivery_id="d",
        kind=EventKind.ISSUE_OPENED,
        repo="o/r",
        actor="alice",
        issue_number=None,
        installation_id=1,
    )
    with pytest.raises(ValueError, match="deepagents_repro_requires_issue_number"):
        await DeepAgentsReproResponder().reproduce_for_event(
            event,
            adapter=_StubAdapter(),  # type: ignore[arg-type]
            sandbox=_StubSandbox(),  # type: ignore[arg-type]
            issue={"title": "t", "body": "b", "base_sha": "abc1234"},
        )


# ── Export contract ─────────────────────────────────────────────────────────


def test_responder_reexported_from_package() -> None:
    """The use case imports from ``openbot.infrastructure.agents`` (not
    the leaf module) — this pin catches an __init__.py regression."""
    from openbot.infrastructure import agents as pkg

    assert hasattr(pkg, "DeepAgentsReproResponder")

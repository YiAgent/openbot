"""Tests for harness-level convergence guards.

These middlewares are the "any model must work" portion of the eval
framework. Tests pin their behaviour without invoking a real LLM —
they construct fake :class:`ToolCallRequest` objects and assert that
the middleware either short-circuits (returning a synthetic ToolMessage)
or delegates to the underlying handler.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from evals.agents.convergence_middleware import (
    ForceCommitBeforeBudget,
    ToolCallRepetitionGuard,
    build_convergence_middlewares,
)


def _request(name: str, args: dict[str, Any], call_id: str = "x") -> ToolCallRequest:
    """Build a minimal ToolCallRequest for testing."""
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": call_id},
        tool=None,
        state={},
        runtime=None,  # type: ignore[arg-type]
    )


def _passthrough_handler(call_log: list[str]) -> Callable[[ToolCallRequest], ToolMessage]:
    """Handler that records the tool-call name and returns a stub OK ToolMessage."""

    def fn(req: ToolCallRequest) -> ToolMessage:
        call_log.append(req.tool_call["name"])
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    return fn


# ─── ToolCallRepetitionGuard ────────────────────────────────────────────────


def test_repetition_guard_lets_first_two_repeats_through() -> None:
    """Threshold=3 means the first two repeats are legitimate (e.g., retry)."""
    guard = ToolCallRepetitionGuard(threshold=3, window=10)
    call_log: list[str] = []
    handler = _passthrough_handler(call_log)

    for _ in range(2):
        result = guard.wrap_tool_call(_request("grep", {"pattern": "foo"}), handler)
        assert not (isinstance(result, ToolMessage) and result.status == "error")
    assert call_log == ["grep", "grep"]


def test_repetition_guard_intercepts_third_identical_call() -> None:
    """Third invocation of the same signature returns a steering ToolMessage."""
    guard = ToolCallRepetitionGuard(threshold=3, window=10)
    call_log: list[str] = []
    handler = _passthrough_handler(call_log)

    # First two repeats pass through; the third trips the guard.
    for _ in range(2):
        guard.wrap_tool_call(_request("grep", {"pattern": "foo"}), handler)
    result = guard.wrap_tool_call(_request("grep", {"pattern": "foo"}), handler)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "REPEATED TOOL CALL DETECTED" in result.content
    # Handler was NOT called for the intercepted invocation.
    assert call_log == ["grep", "grep"]


def test_repetition_guard_distinct_args_do_not_trip() -> None:
    """Same tool name with different args is not a repetition."""
    guard = ToolCallRepetitionGuard(threshold=3, window=10)
    call_log: list[str] = []
    handler = _passthrough_handler(call_log)

    for pattern in ("foo", "bar", "baz", "qux"):
        result = guard.wrap_tool_call(_request("grep", {"pattern": pattern}), handler)
        assert not (isinstance(result, ToolMessage) and result.status == "error")
    assert len(call_log) == 4


def test_repetition_guard_canonicalises_arg_order() -> None:
    """``{a:1, b:2}`` and ``{b:2, a:1}`` must be treated as the same call."""
    guard = ToolCallRepetitionGuard(threshold=3, window=10)
    handler = _passthrough_handler([])

    for args in ({"a": 1, "b": 2}, {"b": 2, "a": 1}, {"a": 1, "b": 2}):
        result = guard.wrap_tool_call(_request("read", args), handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"


def test_repetition_guard_window_evicts_old_calls() -> None:
    """A call that falls out of the window resets its count."""
    guard = ToolCallRepetitionGuard(threshold=3, window=3)
    handler = _passthrough_handler([])

    # Two grep(foo), then 3 distinct calls, then grep(foo) again.
    # By the time we reach the final grep(foo) the prior two are out of
    # the 3-call window → count = 1, no intercept.
    guard.wrap_tool_call(_request("grep", {"p": "foo"}), handler)
    guard.wrap_tool_call(_request("grep", {"p": "foo"}), handler)
    for p in ("bar", "baz", "qux"):
        guard.wrap_tool_call(_request("grep", {"p": p}), handler)
    result = guard.wrap_tool_call(_request("grep", {"p": "foo"}), handler)

    assert not (isinstance(result, ToolMessage) and result.status == "error")


def test_repetition_guard_stops_steering_after_max_steers() -> None:
    """If the agent ignores N steers, fall back to letting the call run."""
    guard = ToolCallRepetitionGuard(threshold=3, window=20, max_steers=1)
    handler = _passthrough_handler([])

    # First 2 calls pass, 3rd intercepts (steer #1, the only one allowed)
    for _ in range(3):
        result = guard.wrap_tool_call(_request("grep", {"p": "foo"}), handler)
    assert isinstance(result, ToolMessage) and result.status == "error"

    # 4th and 5th should be allowed through — no steers left.
    for _ in range(2):
        result = guard.wrap_tool_call(_request("grep", {"p": "foo"}), handler)
        assert not (isinstance(result, ToolMessage) and result.status == "error")


def test_repetition_guard_rejects_bad_config() -> None:
    with pytest.raises(ValueError, match="threshold must be ≥ 2"):
        ToolCallRepetitionGuard(threshold=1)
    with pytest.raises(ValueError, match="window must be ≥ threshold"):
        ToolCallRepetitionGuard(threshold=5, window=3)


# ─── ForceCommitBeforeBudget ────────────────────────────────────────────────


def test_force_commit_fires_at_threshold() -> None:
    """At 70% of a 10-call budget (call 7), the wrap-up message is injected."""
    fc = ForceCommitBeforeBudget(model_call_limit=10, commit_at_fraction=0.7)
    # Calls 1-6 are below the threshold (commit_threshold = 7).
    for _ in range(6):
        assert fc.before_model({}, None) is None
    # Call 7 fires the steer.
    patch = fc.before_model({}, None)
    assert patch is not None
    assert "messages" in patch
    msg = patch["messages"][0]
    assert "BUDGET HEADS-UP" in msg.content
    assert "7/10" in msg.content


def test_force_commit_fires_at_most_once() -> None:
    """The steering message is single-shot — repeated triggers are no-ops."""
    fc = ForceCommitBeforeBudget(model_call_limit=10, commit_at_fraction=0.5)
    # Burn through enough calls to fire.
    for _ in range(5):
        fc.before_model({}, None)
    # Subsequent calls return None even though we're past the threshold.
    for _ in range(3):
        assert fc.before_model({}, None) is None


def test_force_commit_rejects_bad_config() -> None:
    with pytest.raises(ValueError, match="model_call_limit"):
        ForceCommitBeforeBudget(model_call_limit=0)
    with pytest.raises(ValueError, match="commit_at_fraction"):
        ForceCommitBeforeBudget(model_call_limit=10, commit_at_fraction=1.5)
    with pytest.raises(ValueError, match="commit_at_fraction"):
        ForceCommitBeforeBudget(model_call_limit=10, commit_at_fraction=0)


# ─── Async paths ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repetition_guard_async_path_intercepts() -> None:
    guard = ToolCallRepetitionGuard(threshold=3, window=10)
    saw: list[str] = []

    async def ahandler(req: ToolCallRequest) -> ToolMessage:
        saw.append(req.tool_call["name"])
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    for _ in range(3):
        result = await guard.awrap_tool_call(_request("grep", {"p": "x"}), ahandler)
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert saw == ["grep", "grep"]  # handler not called on the 3rd


@pytest.mark.asyncio
async def test_force_commit_async_path_fires() -> None:
    fc = ForceCommitBeforeBudget(model_call_limit=4, commit_at_fraction=0.5)
    for _ in range(1):
        assert await fc.abefore_model({}, None) is None
    patch = await fc.abefore_model({}, None)
    assert patch is not None
    assert "2/4" in patch["messages"][0].content


# ─── Builder shape ─────────────────────────────────────────────────────────


def test_builder_returns_repetition_then_force_commit_in_order() -> None:
    """Repetition guard must come before ForceCommit so tool-level intercepts
    don't have to traverse a model-level middleware first.
    """
    mws = build_convergence_middlewares(model_call_limit=20)
    assert len(mws) == 2
    assert isinstance(mws[0], ToolCallRepetitionGuard)
    assert isinstance(mws[1], ForceCommitBeforeBudget)


def test_builder_propagates_config() -> None:
    mws = build_convergence_middlewares(
        model_call_limit=30,
        repetition_threshold=5,
        repetition_window=20,
        repetition_max_steers=4,
        commit_at_fraction=0.5,
    )
    guard, fc = mws
    assert isinstance(guard, ToolCallRepetitionGuard)
    assert guard._threshold == 5
    assert guard._window.maxlen == 20
    assert guard._steers_remaining == 4
    assert isinstance(fc, ForceCommitBeforeBudget)
    assert fc._model_call_limit == 30
    assert fc._commit_threshold == 15  # 30 * 0.5

"""Unit tests for evals.agents.middleware.

These pin the failure-mode coverage that the old per-task force-retry
helpers used to provide. The finalizer is the *one* place we enforce
structured output, so every meaningful corner — empty trace, dict
return shape, validation failure, runnable boom — lands here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from evals.agents.middleware import (
    _StructuredFinalizerWrapper,
    finalize_structured,
    finalize_structured_sync,
    wrap_agent_with_finalizer,
)
from evals.common.termination import AgentTerminationError


class _Answer(BaseModel):
    text: str


def _ai(content: str) -> AIMessage:
    return AIMessage(content=content)


# ─── prose collection / empty-trace gate ──────────────────────────────────


def test_finalize_sync_raises_when_no_prose() -> None:
    """Empty trace → AgentTerminationError, no LLM call.

    The wrapper relies on this contract so empty-message degraded runs
    surface as errored samples instead of being papered over with a
    synthetic schema instance.
    """
    with pytest.raises(AgentTerminationError, match="no agent prose"):
        finalize_structured_sync([], schema=_Answer, model="anthropic:any")


def test_finalize_sync_raises_when_messages_have_only_tool_or_human() -> None:
    """Non-AI messages don't count as prose.

    Budget-cut traces sometimes end with a HumanMessage from the
    ``ForceCommitBeforeBudget`` middleware, or a ToolMessage from a
    tool-call that the budget interrupted. Either way, no AI text =
    no schema.
    """
    from langchain_core.messages import HumanMessage, ToolMessage

    messages: list[Any] = [
        HumanMessage(content="hint"),
        ToolMessage(content="tool result", tool_call_id="t1"),
    ]
    with pytest.raises(AgentTerminationError, match="no agent prose"):
        finalize_structured_sync(messages, schema=_Answer, model="anthropic:any")


# ─── shape handling: model instance, dict, wrong type ────────────────────


def test_finalize_sync_passes_through_pydantic_instance() -> None:
    """Runnable that returns the schema instance directly is the happy path."""

    class _Stub:
        def invoke(self, _prompt):
            return _Answer(text="hello")

    with patch(
        "evals.agents.middleware._build_finalizer_runnable",
        return_value=_Stub(),
    ):
        out = finalize_structured_sync([_ai("hello")], schema=_Answer, model="anthropic:any")
    assert isinstance(out, _Answer)
    assert out.text == "hello"


def test_finalize_sync_validates_dict_return() -> None:
    """Some langchain providers return a dict — coerced through ``model_validate``."""

    class _Stub:
        def invoke(self, _prompt):
            return {"text": "from-dict"}

    with patch(
        "evals.agents.middleware._build_finalizer_runnable",
        return_value=_Stub(),
    ):
        out = finalize_structured_sync([_ai("prose")], schema=_Answer, model="anthropic:any")
    assert isinstance(out, _Answer)
    assert out.text == "from-dict"


def test_finalize_sync_raises_on_unvalidatable_dict() -> None:
    """Dict that doesn't fit the schema → ``AgentTerminationError``."""

    class _Stub:
        def invoke(self, _prompt):
            return {"wrong_key": "x"}

    with (
        patch(
            "evals.agents.middleware._build_finalizer_runnable",
            return_value=_Stub(),
        ),
        pytest.raises(AgentTerminationError, match="failed _Answer validation"),
    ):
        finalize_structured_sync([_ai("prose")], schema=_Answer, model="anthropic:any")


def test_finalize_sync_raises_on_unexpected_return_type() -> None:
    """Runnable returning a bare string is a provider-shape mismatch — fail loud."""

    class _Stub:
        def invoke(self, _prompt):
            return "just a string"

    with (
        patch(
            "evals.agents.middleware._build_finalizer_runnable",
            return_value=_Stub(),
        ),
        pytest.raises(AgentTerminationError, match="unexpected type"),
    ):
        finalize_structured_sync([_ai("prose")], schema=_Answer, model="anthropic:any")


def test_finalize_sync_wraps_provider_exception() -> None:
    """Transport / quota / auth errors surface as termination errors, not raw Exceptions.

    Solvers raise ``AgentTerminationError`` to Inspect; raw HTTP errors
    would bypass that contract and pollute the sample's error category.
    """

    class _Boom:
        def invoke(self, _prompt):
            raise RuntimeError("provider 500")

    with (
        patch(
            "evals.agents.middleware._build_finalizer_runnable",
            return_value=_Boom(),
        ),
        pytest.raises(AgentTerminationError, match="provider 500"),
    ):
        finalize_structured_sync([_ai("prose")], schema=_Answer, model="anthropic:any")


# ─── async twin behaves the same on the happy path ─────────────────────────


@pytest.mark.asyncio
async def test_finalize_async_happy_path() -> None:
    """``finalize_structured`` and ``finalize_structured_sync`` agree."""

    class _Stub:
        async def ainvoke(self, _prompt):
            return _Answer(text="async")

    with patch(
        "evals.agents.middleware._build_finalizer_runnable",
        return_value=_Stub(),
    ):
        out = await finalize_structured([_ai("prose")], schema=_Answer, model="anthropic:any")
    assert isinstance(out, _Answer)
    assert out.text == "async"


# ─── wrapper behaviour: structured_response is always populated ────────────


def test_wrapper_invoke_skips_finalizer_when_upstream_supplied_schema() -> None:
    """Test fakes that already set ``structured_response`` skip the LLM call.

    Production deepagents never sets ``structured_response`` (we no
    longer pass ``response_format`` into the loop), so the LLM
    finalizer always runs in production. But test fakes do, and we
    must not require credentials to verify solver wiring.
    """
    pre = _Answer(text="from-stub")

    class _Agent:
        def invoke(self, _payload):
            return {"messages": [_ai("ignored")], "structured_response": pre}

    wrapper = wrap_agent_with_finalizer(_Agent(), schema=_Answer, model="anthropic:any")
    with patch(
        "evals.agents.middleware.finalize_structured_sync",
        side_effect=AssertionError("should not run when upstream supplied schema"),
    ):
        result = wrapper.invoke({})
    assert result["structured_response"] is pre


def test_wrapper_invoke_runs_finalizer_when_upstream_lacks_schema() -> None:
    """No upstream ``structured_response`` → finalizer fires and populates it."""

    class _Agent:
        def invoke(self, _payload):
            return {"messages": [_ai("agent prose")]}

    recovered = _Answer(text="recovered")
    wrapper = wrap_agent_with_finalizer(_Agent(), schema=_Answer, model="anthropic:any")
    with patch(
        "evals.agents.middleware.finalize_structured_sync",
        return_value=recovered,
    ):
        result = wrapper.invoke({})
    assert result["structured_response"] is recovered


def test_wrapper_forwards_unknown_attrs_to_underlying_agent() -> None:
    """The wrapper duck-types CompiledStateGraph via ``__getattr__``."""

    class _Agent:
        def invoke(self, _payload):  # required for wrapper construction
            return {"messages": []}

        def get_state(self):
            return "state-sentinel"

    wrapper: _StructuredFinalizerWrapper[_Answer] = wrap_agent_with_finalizer(
        _Agent(), schema=_Answer, model="anthropic:any"
    )
    assert wrapper.get_state() == "state-sentinel"  # type: ignore[attr-defined]

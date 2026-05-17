"""Unit tests for :mod:`evals.common.termination`.

These pin the *clean-termination contract* — the gate every deepagents
solver runs after ``agent.ainvoke()`` to refuse to score a sample
whose agent didn't actually finish.

The contract is asymmetric on purpose:

  - In **structured-response mode** the gate is satisfied by a parsed
    Pydantic payload on ``result["structured_response"]``. The
    schema-bound terminal step often leaves an empty-text ``AIMessage``
    in the tail (the tool_call carrying the structured payload), so
    forcing a content check there would reject genuine clean
    terminations.
  - In **prose mode** (``requires_structured_response=False``) the
    gate inspects the tail message: it must be the model's own
    ``AIMessage`` emission, with no pending ``tool_calls`` and with
    non-whitespace text. A synthetic ``ToolMessage`` left by
    ``ToolCallLimitMiddleware`` or a ``HumanMessage`` from
    ``ForceCommitBeforeBudget`` would land in the tail when the graph
    is cut mid-flight — both produce ``getattr(last, "type", None) !=
    "ai"`` and are rejected.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from evals.common.termination import AgentTerminationError, assert_clean_termination


class _AiTail:
    """Minimal AI-tail stub mirroring langchain ``AIMessage`` duck-typing."""

    type = "ai"

    def __init__(
        self,
        content: Any = "ok",
        *,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        self.content = content
        if tool_calls is not None:
            self.tool_calls = tool_calls


class _ToolTail:
    """Stand-in for a synthetic ToolMessage left by a budget-exhausted middleware."""

    type = "tool"
    content = "Tool budget exhausted, finish with what you have."


class _HumanTail:
    """Stand-in for the ``ForceCommitBeforeBudget`` injected HumanMessage."""

    type = "human"
    content = "BUDGET HEADS-UP: emit your final answer now."


class _Answer(BaseModel):
    body: str


def test_empty_message_log_raises() -> None:
    with pytest.raises(AgentTerminationError, match="no messages"):
        assert_clean_termination({"messages": []})


def test_missing_messages_key_raises() -> None:
    with pytest.raises(AgentTerminationError, match="no messages"):
        assert_clean_termination({})


def test_clean_ai_tail_passes() -> None:
    assert_clean_termination({"messages": [_AiTail(content="final answer")]})


def test_tool_tail_raises() -> None:
    """A synthetic ToolMessage in the tail = middleware-cut, not a real answer."""
    with pytest.raises(AgentTerminationError, match="tool"):
        assert_clean_termination({"messages": [_AiTail(), _ToolTail()]})


def test_human_tail_raises() -> None:
    """A HumanMessage in the tail = ForceCommitBeforeBudget fired but the model never replied."""
    with pytest.raises(AgentTerminationError, match="human"):
        assert_clean_termination({"messages": [_AiTail(), _HumanTail()]})


def test_pending_tool_calls_raises() -> None:
    """Model asked for more tools but the graph cut before they ran."""
    tail = _AiTail(
        content="",
        tool_calls=[{"name": "grep", "args": {"q": "TODO"}, "id": "call-1"}],
    )
    with pytest.raises(AgentTerminationError, match="pending tool_calls"):
        assert_clean_termination({"messages": [tail]})


def test_empty_string_content_raises() -> None:
    with pytest.raises(AgentTerminationError, match="no visible text"):
        assert_clean_termination({"messages": [_AiTail(content="")]})


def test_whitespace_only_content_raises() -> None:
    with pytest.raises(AgentTerminationError, match="no visible text"):
        assert_clean_termination({"messages": [_AiTail(content="   \n\t  ")]})


def test_anthropic_content_blocks_with_text_passes() -> None:
    """LangChain Anthropic content can be ``list[{type, text}]`` — accept it."""
    blocks = [
        {"type": "text", "text": "Here's my analysis."},
        {"type": "text", "text": "Final answer: 42."},
    ]
    assert_clean_termination({"messages": [_AiTail(content=blocks)]})


def test_empty_content_blocks_raises() -> None:
    blocks: list[dict[str, str]] = [{"type": "text", "text": ""}]
    with pytest.raises(AgentTerminationError, match="no visible text"):
        assert_clean_termination({"messages": [_AiTail(content=blocks)]})


def test_structured_mode_skips_tail_message_check() -> None:
    """An empty-text AI tail is fine when the structured payload carries the answer."""
    sr = _Answer(body="final")
    assert_clean_termination(
        {"messages": [_AiTail(content="")], "structured_response": sr},
        requires_structured_response=True,
        structured_response_type=_Answer,
    )


def test_structured_mode_missing_response_raises() -> None:
    with pytest.raises(AgentTerminationError, match="structured response"):
        assert_clean_termination(
            {"messages": [_AiTail(content="prose only")]},
            requires_structured_response=True,
        )


def test_structured_mode_accepts_dict_payload() -> None:
    """LangChain provider strategies sometimes hand us a dict — solver re-validates."""
    assert_clean_termination(
        {
            "messages": [_AiTail(content="")],
            "structured_response": {"body": "final"},
        },
        requires_structured_response=True,
        structured_response_type=_Answer,
    )


def test_structured_mode_rejects_wrong_type() -> None:
    with pytest.raises(AgentTerminationError, match="unexpected type"):
        assert_clean_termination(
            {
                "messages": [_AiTail(content="")],
                "structured_response": "raw string is neither Pydantic nor dict",
            },
            requires_structured_response=True,
            structured_response_type=_Answer,
        )


def test_structured_mode_still_requires_messages() -> None:
    """A run that produced no messages at all is always errored, even with structured_response."""
    with pytest.raises(AgentTerminationError, match="no messages"):
        assert_clean_termination(
            {"messages": [], "structured_response": _Answer(body="x")},
            requires_structured_response=True,
            structured_response_type=_Answer,
        )

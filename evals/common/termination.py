"""Termination contract for deepagents-based eval solvers.

The harness-level middlewares (``ToolCallLimitMiddleware``,
``ModelCallLimitMiddleware``, :class:`ToolCallRepetitionGuard`) and the
LangChain SDK retry layer can produce a *degraded* terminal state — an
empty / partial ``AIMessage``, a missing ``structured_response``, or a
dangling tool-call request — when the agent runs out of budget, the
provider rate-limits us mid-flight, or the convergence guards steer
the model into giving up. Scoring such states as real candidate
outputs pollutes the metric: the judge sees a half-finished answer
and grades it as a genuine but bad one.

This module gives each solver a single gatekeeper to call after
``agent.ainvoke()``:

    assert_clean_termination(
        result,
        requires_structured_response=True,
        structured_response_type=SweQaProAnswer,
    )

If the run did not terminate cleanly, the helper raises
:class:`AgentTerminationError`. Solvers propagate it (no
``try/except``); Inspect catches it as an errored sample, retries per
``--retry-on-error``, and ultimately excludes it from the metric **and**
from the LangSmith feedback upload under ``--fail-on-error``. The
judge is never called for such samples.

Clean termination means:

1. The result carries a non-empty ``messages`` list. An empty log
   means the sandbox / transport failed before any LLM call landed.
2. When ``requires_structured_response=True``: the result also
   carries a ``structured_response`` of the configured Pydantic-model
   shape. The prior "protocol-violation fallback" — synthesising a
   structured answer from tail prose — is gone because it conflated
   infra failure with a real-but-poor model answer. We *don't* check
   the message tail in structured mode, because the schema-bound
   terminal step often emits an ``AIMessage`` with empty text and a
   single tool_call carrying the structured payload; LangChain
   reflects that into ``structured_response`` for us.
3. When ``requires_structured_response=False``: the last message is
   an :class:`AIMessage` — the model's own emission, not a synthetic
   ``ToolMessage`` left by a budget-exhausted middleware. That last
   ``AIMessage`` has visible non-whitespace text content and carries
   no pending ``tool_calls`` (a model that asked for more tools but
   was cut off mid-flight never reached its planned terminal step).
"""

from __future__ import annotations

from typing import Any


class AgentTerminationError(RuntimeError):
    """Raised when an agent run did not reach a clean terminal state.

    Inspect AI catches this in the solver, treats the sample as
    errored, and excludes it from the metric aggregate and the
    LangSmith feedback upload (per ``--fail-on-error`` /
    ``--retry-on-error`` semantics). The judge is never called for
    such samples.
    """


def assert_clean_termination(
    result: dict[str, Any],
    *,
    requires_structured_response: bool = False,
    structured_response_type: type | None = None,
) -> None:
    """Verify the agent run terminated normally; raise otherwise.

    Args:
        result: The dict returned by ``agent.ainvoke({...})``.
        requires_structured_response: When ``True``, demand
            ``result["structured_response"]`` is present and (if
            ``structured_response_type`` is given) is an instance of
            that type or a dict pydantic could validate into it. Use
            ``False`` for tasks that don't bind a ``response_format``
            (swe_fix / swe_test).
        structured_response_type: Optional Pydantic model class. When
            provided, the structured response must be an instance of
            that class or a dict — bare ``None`` / a wrong shape
            raises.

    Raises:
        AgentTerminationError: When the agent did not produce a clean
            terminal ``AIMessage``, or (for structured tasks) failed
            to emit the required structured response.
    """
    messages = list(result.get("messages") or [])
    if not messages:
        raise AgentTerminationError(
            "agent produced no messages — sandbox or transport failure before any LLM call"
        )

    if requires_structured_response:
        sr = result.get("structured_response")
        if sr is None:
            raise AgentTerminationError(
                "agent did not emit a structured response — required by "
                "task contract but the schema-bound terminal step never ran"
            )
        if structured_response_type is not None and not isinstance(
            sr, structured_response_type | dict
        ):
            raise AgentTerminationError(
                f"structured_response has unexpected type {type(sr).__name__}; "
                f"expected {structured_response_type.__name__} or dict"
            )
        # Structured mode is satisfied by the schema-bound payload alone —
        # the message tail is often an empty-text AIMessage carrying the
        # final tool_call, which would (correctly) fail the non-structured
        # checks below. Skip them.
        return

    last = messages[-1]
    # Duck-type the AI-message check via langchain's ``BaseMessage.type``
    # discriminator (``"ai"`` / ``"tool"`` / ``"human"`` / ``"system"``).
    # Real :class:`AIMessage` instances set ``type == "ai"``; a synthetic
    # :class:`ToolMessage` left by a budget-exhausted middleware would
    # surface as ``"tool"``, and ``ForceCommitBeforeBudget`` injects a
    # :class:`HumanMessage` (``"human"``). Anything tail-positioned that
    # is not ``"ai"`` means the graph terminated before the model got a
    # turn to respond — refuse to score.
    if getattr(last, "type", None) != "ai":
        raise AgentTerminationError(
            f"agent's last message is type={getattr(last, 'type', '?')!r}, not 'ai' — "
            "graph ended mid-tool-call (budget cap or middleware intervention)"
        )

    if getattr(last, "tool_calls", None):
        raise AgentTerminationError(
            "agent's last AIMessage has pending tool_calls — model wanted "
            "more tools but the graph terminated before they ran"
        )

    if not _has_visible_text(last):
        raise AgentTerminationError(
            "agent's last AIMessage has no visible text content — empty "
            "final answer, likely middleware-cut mid-thought"
        )


def _has_visible_text(message: Any) -> bool:
    """Return True iff the message carries non-whitespace text content."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or ""
                if isinstance(text, str) and text.strip():
                    return True
        return False
    return False


__all__ = ["AgentTerminationError", "assert_clean_termination"]

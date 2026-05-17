"""Absolute structured-output finalizer for deepagents-based eval solvers.

The deepagents loop binds ``response_format`` via LangChain's
``AutoStrategy`` — Anthropic's forced tool-call mechanism
(``tool_choice={type: "tool", name: "<schema>"}``). Two failure modes
follow from that coupling:

1. **Forced-tool + thinking conflict.** Anthropic rejects
   ``tool_choice=forced`` together with ``thinking={"type": "enabled"}``,
   so LangChain's compat shim silently drops ``tool_choice``. The schema
   tool becomes optional and the model often skips it.

2. **Mid-loop AIMessage exit.** deepagents is a multi-step graph;
   ``response_format`` only binds the *terminal* step. If a planner /
   sub-agent step finishes with an AIMessage that has no ``tool_calls``,
   the graph terminates *before* the schema-bound step ever runs.

Both modes used to be "papered over" by a per-task force-retry
(``review.py``) or fail-the-sample contract (``swe_qa.py``). Neither is
free — the metric loses samples and we waste tokens on retries.

This module makes structured output **architecturally guaranteed** by
decoupling the agent loop from schema enforcement:

- The agent loop runs *without* ``response_format`` — thinking stays on,
  no forced-tool conflict, the model freely explores.
- After ``ainvoke`` / ``invoke``, :func:`finalize_structured` makes a
  single dedicated LLM call: ``tool_choice=forced`` + ``thinking=0`` +
  **no other tools**. With one tool to choose from and the constraint
  hard, Anthropic must emit that tool call. The schema is recovered
  from the agent's prose tail (last few KB carry the answer; older
  context is exploration noise).

Use :func:`wrap_agent_with_finalizer` to bolt this onto any deepagents
agent — the returned object preserves ``ainvoke`` / ``invoke`` shape
and injects ``structured_response`` into the result dict, so solvers
that already read ``result["structured_response"]`` continue to work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from evals.common.deepagents_baseline import build_chat_model
from evals.common.termination import AgentTerminationError

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

# Empirical cap. Review prose runs ~1M tokens with sandbox traces; the
# enumerated findings always live in the tail. Cloning the full
# transcript into the finalizer call doubles cost for no quality gain.
_DEFAULT_PROSE_TAIL_CHARS = 12_000

_FINALIZER_PROMPT = """\
You just produced an analysis (shown below) but did NOT call the required
structured-response schema. Re-emit your answer now via the schema tool.
Use ONLY the content you already produced in the analysis below — do not
invent new facts, do not omit anything. If your analysis concluded the
schema's payload should be empty (e.g. an empty findings list), emit
that empty payload exactly.

YOUR PREVIOUS ANALYSIS
----------------------
{prose}
"""

SchemaT = TypeVar("SchemaT", bound=BaseModel)


def _collect_prose(messages: list[BaseMessage]) -> str:
    """Concatenate visible text from AIMessages, skipping tool plumbing."""
    parts: list[str] = []
    for msg in messages:
        if getattr(msg, "type", None) != "ai":
            continue
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            if content.strip():
                parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or ""
                    if isinstance(text, str) and text.strip():
                        parts.append(text)
    return "\n".join(parts)


def _truncate_prose(prose: str, max_chars: int) -> str:
    if len(prose) <= max_chars:
        return prose
    return "…[earlier analysis truncated]…\n" + prose[-max_chars:]


def _build_finalizer_runnable(model: str, schema: type[SchemaT]) -> Any:
    return build_chat_model(model, thinking_budget_tokens=0).with_structured_output(schema)


def _validate_finalizer_output(parsed: Any, schema: type[SchemaT]) -> SchemaT:
    if isinstance(parsed, schema):
        return parsed
    if isinstance(parsed, dict):
        try:
            return schema.model_validate(parsed)
        except Exception as exc:
            raise AgentTerminationError(
                f"structured finalizer returned dict that failed {schema.__name__} "
                f"validation: {exc}"
            ) from exc
    raise AgentTerminationError(
        f"structured finalizer returned unexpected type {type(parsed).__name__}; "
        f"expected {schema.__name__} or dict"
    )


def _prepare_prompt(messages: list[BaseMessage], prose_tail_chars: int) -> str:
    prose = _collect_prose(messages)
    if not prose.strip():
        raise AgentTerminationError(
            "structured finalizer received no agent prose — graph terminated "
            "before any AIMessage carried visible text"
        )
    return _FINALIZER_PROMPT.format(prose=_truncate_prose(prose, prose_tail_chars))


def finalize_structured_sync(
    messages: list[BaseMessage],
    *,
    schema: type[SchemaT],
    model: str,
    prose_tail_chars: int = _DEFAULT_PROSE_TAIL_CHARS,
) -> SchemaT:
    """Synchronous twin of :func:`finalize_structured`.

    Used by :meth:`_StructuredFinalizerWrapper.invoke` when the caller
    runs inside an already-active event loop (Inspect's solver harness,
    pytest-asyncio fixtures) — ``asyncio.run`` would explode there.
    """
    prompt = _prepare_prompt(messages, prose_tail_chars)
    runnable = _build_finalizer_runnable(model, schema)
    try:
        parsed = runnable.invoke(prompt)
    except Exception as exc:
        raise AgentTerminationError(
            f"structured finalizer call failed: {type(exc).__name__}: {exc}"
        ) from exc
    return _validate_finalizer_output(parsed, schema)


async def finalize_structured(
    messages: list[BaseMessage],
    *,
    schema: type[SchemaT],
    model: str,
    prose_tail_chars: int = _DEFAULT_PROSE_TAIL_CHARS,
) -> SchemaT:
    """Convert an agent's prose tail into a validated schema instance.

    Single dedicated LLM call with ``tool_choice=forced`` + ``thinking=0``
    bound to ``schema``. No other tools are exposed, so the Anthropic
    forced-tool constraint is physically unambiguous: the model must
    emit that tool call.

    Args:
        messages: ``result["messages"]`` from the deepagents loop.
        schema: Pydantic model class describing the required shape.
        model: ``provider:model`` id — same one the agent ran on, so the
            finalizer doesn't need its own quota / billing identity.
        prose_tail_chars: Cap on how much of the prose tail we replay
            into the finalizer. Default 12k chars.

    Returns:
        A validated instance of ``schema``.

    Raises:
        AgentTerminationError: When the finalizer call itself fails
            (transport error, validation error, quota exhausted). Solvers
            propagate this to Inspect, which treats the sample as errored
            — same termination contract as before, but the trigger is
            *real* failure, not a fragile schema-binding race.
    """
    prompt = _prepare_prompt(messages, prose_tail_chars)
    runnable = _build_finalizer_runnable(model, schema)
    try:
        parsed = await runnable.ainvoke(prompt)
    except Exception as exc:
        raise AgentTerminationError(
            f"structured finalizer call failed: {type(exc).__name__}: {exc}"
        ) from exc
    return _validate_finalizer_output(parsed, schema)


class _StructuredFinalizerWrapper(Generic[SchemaT]):
    """Wraps a deepagents compiled graph so structured output is guaranteed.

    Only ``ainvoke`` and ``invoke`` are intercepted — that's all our
    solvers use. Both call into the underlying agent first (no
    ``response_format`` bound), then run :func:`finalize_structured` on
    the messages, and store the parsed schema under
    ``result["structured_response"]`` so downstream solver code is
    unchanged.
    """

    def __init__(self, agent: Any, *, schema: type[SchemaT], model: str) -> None:
        self._agent = agent
        self._schema = schema
        self._model = model

    def __getattr__(self, name: str) -> Any:
        # Forward anything we don't intercept to the underlying agent.
        return getattr(self._agent, name)

    async def ainvoke(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        forward = self._forward_kwargs(config, kwargs)
        ainvoke = getattr(self._agent, "ainvoke", None)
        if ainvoke is not None:
            result = await ainvoke(input, **forward)
        else:
            # Sync-only stubs (test fakes): run the sync path on a thread
            # so we don't block the event loop.
            import asyncio

            result = await asyncio.to_thread(self._agent.invoke, input, **forward)
        return await self._finalize(result)

    def invoke(
        self,
        input: Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Sync path — solvers can be running inside ``loop.run_until_complete``
        # (Inspect's harness, pytest-asyncio), where ``asyncio.run`` would
        # raise. We use the sync finalizer directly.
        result = self._agent.invoke(input, **self._forward_kwargs(config, kwargs))
        messages = list(result.get("messages") or []) if isinstance(result, dict) else []
        existing = result.get("structured_response") if isinstance(result, dict) else None
        parsed = self._reuse_or_finalize_sync(existing, messages)
        if isinstance(result, dict):
            result["structured_response"] = parsed
        return result

    def _reuse_or_finalize_sync(self, existing: Any, messages: list[Any]) -> Any:
        """Reuse an upstream structured_response when present, else call the sync finalizer.

        Test fakes set ``structured_response`` directly on the stub
        agent's output — running the LLM finalizer over those would
        require credentials and break offline tests. Real deepagents
        never sets ``structured_response`` because we no longer pass
        ``response_format`` into it, so the production path always
        flows through the finalizer.
        """
        if isinstance(existing, self._schema):
            return existing
        if isinstance(existing, dict):
            return self._schema.model_validate(existing)
        return finalize_structured_sync(messages, schema=self._schema, model=self._model)

    @staticmethod
    def _forward_kwargs(config: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Build the kwargs passed to the underlying agent, omitting empties.

        Test stubs typically expose ``invoke(input)`` with no ``config=`` —
        forwarding ``config=None`` would TypeError. Real LangGraph
        ``CompiledStateGraph.invoke`` accepts ``config`` keyword. We
        only pass it when it's non-None.
        """
        forward: dict[str, Any] = dict(kwargs)
        if config is not None:
            forward["config"] = config
        return forward

    async def _finalize(self, result: Any) -> dict[str, Any]:
        messages = list(result.get("messages") or []) if isinstance(result, dict) else []
        existing = result.get("structured_response") if isinstance(result, dict) else None
        if isinstance(existing, self._schema):
            parsed: Any = existing
        elif isinstance(existing, dict):
            parsed = self._schema.model_validate(existing)
        else:
            parsed = await finalize_structured(messages, schema=self._schema, model=self._model)
        # Mirror the deepagents output shape: the parsed schema goes into
        # ``structured_response``. ``assert_clean_termination`` reads it
        # from there.
        if isinstance(result, dict):
            result["structured_response"] = parsed
        return result


def wrap_agent_with_finalizer(
    agent: Any,
    *,
    schema: type[SchemaT],
    model: str,
) -> _StructuredFinalizerWrapper[SchemaT]:
    """Return a wrapper whose ``ainvoke`` / ``invoke`` guarantee structured output.

    The wrapper is duck-typed against ``CompiledStateGraph`` — anything
    we don't intercept is forwarded via ``__getattr__``, so streaming,
    state inspection, etc. still work on the underlying agent.
    """
    return _StructuredFinalizerWrapper(agent, schema=schema, model=model)


__all__ = [
    "finalize_structured",
    "wrap_agent_with_finalizer",
]

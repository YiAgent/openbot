"""OpenBot's shared ``deepagents`` baseline — single config for all eval solvers.

Every eval solver that drives ``deepagents`` (review, fix, future tasks) goes
through this module so the agent shape stays consistent:

- Same :class:`deepagents.HarnessProfileConfig` per model:
  - ``excluded_tools={"write_todos"}`` — single-issue eval tasks don't need
    a planning todo list.
  - ``general_purpose_subagent.enabled=False`` — drops the auto-attached
    ``task`` tool so the agent doesn't try to delegate to itself.
- Same model resolution rule: explicit arg → shared
  ``OPENBOT_DEEPAGENTS_MODEL`` env → fallback, with auto ``anthropic:``
  prefix when missing (deepagents / langchain expect ``provider:model``).
- Same ``RunnableConfig`` shape for LangSmith trace naming + metadata.

Per-task differences (system prompt, backend, extra tools) stay in the
caller — this module exposes the knobs without baking them in.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.protocol import BackendProtocol
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfileConfig,
    register_harness_profile,
)
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.agents.structured_output import (
    AutoStrategy,
    ProviderStrategy,
    ToolStrategy,
)
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from evals.common import config
from evals.common.config import get_eval_config

# Type alias for the `response_format` knob — mirrors deepagents' signature.
ResponseFormat = (
    type[Any]
    | dict[str, Any]
    | AutoStrategy[Any]
    | ProviderStrategy[Any]
    | ToolStrategy[Any]
    | None
)

# All env-var names + numeric defaults are centralised in
# :mod:`evals.common.config`. The accessors below stay so callers keep a
# stable, typed API; they delegate every read to ``config``. The empirical
# rationale behind each default — observed smoke-run envelopes, the
# CLOSE_WAIT-hang fix, and the truncated thinking-model output bug —
# is documented next to the values in ``config.py`` and in this module's
# top-level docstring.


def get_model_call_limit() -> int:
    """Max LLM calls per Inspect sample."""
    return get_eval_config().deepagents.model_call_limit


def get_tool_call_limit() -> int:
    """Max tool invocations per Inspect sample."""
    return get_eval_config().deepagents.tool_call_limit


def get_recursion_limit() -> int:
    """LangGraph hard step backstop.

    Set well above the tool / model limits because LangGraph counts every
    node transition (tool prep, tool result merge, etc.), not just LLM
    calls. Treat as a last-resort guard; tuned middlewares should
    converge first.
    """
    return get_eval_config().deepagents.recursion_limit


def get_model_timeout_s() -> int:
    """Per-request HTTP timeout in seconds.

    Applied to the provider's httpx client. Pydantic's ``PositiveInt``
    rejects ``0`` at load time (would mean "no timeout", which is the
    bug we're fixing) — invalid env values now fail loud at startup
    instead of silently falling back.
    """
    return get_eval_config().deepagents.model_timeout_s


def get_model_max_retries() -> int:
    """HTTP-layer retry count for transient provider errors.

    Provider SDKs retry on 429 / 5xx / connection errors with
    exponential backoff. Sample-layer retries (Inspect
    ``--retry-on-error``) sit on top of this for errors the HTTP layer
    can't recover from.
    """
    return get_eval_config().deepagents.model_max_retries


def get_max_output_tokens() -> int:
    """Per-call max output tokens.

    Provider defaults are typically 4096 — too tight for thinking-style
    models that burn the whole budget on chain-of-thought before they
    can emit their final answer.
    """
    return get_eval_config().deepagents.max_output_tokens


def get_thinking_level() -> str:
    """Configured extended-thinking tier (``off`` | ``low`` | ``medium`` | ``high``).

    Read through pydantic Settings so an invalid env value surfaces at
    startup (not as a silent fall-through). The numeric budget is looked
    up via :data:`config.THINKING_LEVEL_BUDGETS`.
    """
    return get_eval_config().deepagents.thinking_level


def get_thinking_budget_tokens() -> int:
    """Resolve the configured thinking level to an Anthropic ``budget_tokens``.

    Returns ``0`` for ``off`` — callers should treat 0 as "skip the
    thinking field entirely" so non-Anthropic gateways that don't
    understand the parameter don't receive it.
    """
    return config.THINKING_LEVEL_BUDGETS.get(get_thinking_level(), 0)


def build_chat_model(
    model: str,
    *,
    timeout_s: int | None = None,
    max_retries: int | None = None,
    max_output_tokens: int | None = None,
    thinking_budget_tokens: int | None = None,
) -> BaseChatModel:
    """Construct a chat model with explicit timeout + retry + output-cap knobs.

    deepagents accepts either a ``provider:model`` string or a constructed
    :class:`BaseChatModel`. We always go through the latter so the HTTP
    client carries our timeout and ``max_retries`` config — passing only
    the string lets the provider SDK fall back to its defaults, which on
    httpx means *no read timeout at all*.

    ``max_tokens`` is propagated because LangChain's Anthropic-compatible
    providers default to 4096 — thinking-style models silently truncate
    their final answer when the entire output budget is spent on
    chain-of-thought. See :func:`get_max_output_tokens`.

    Args:
        model: ``provider:model`` id (already normalised via :func:`resolve_model`).
        timeout_s: HTTP request timeout in seconds. ``None`` → :func:`get_model_timeout_s`.
        max_retries: HTTP-layer retry count. ``None`` → :func:`get_model_max_retries`.
        max_output_tokens: Provider-side ``max_tokens`` per call. ``None`` →
            :func:`get_max_output_tokens`.
        thinking_budget_tokens: Anthropic extended-thinking ``budget_tokens``.
            ``None`` → :func:`get_thinking_budget_tokens` (env-overridable).
            A value of ``0`` disables thinking — required for gateways
            that don't honour the parameter.

    Returns:
        A LangChain ``BaseChatModel`` ready to hand to ``create_deep_agent``.
    """
    resolved_thinking = (
        thinking_budget_tokens
        if thinking_budget_tokens is not None
        else get_thinking_budget_tokens()
    )
    # Anthropic SDK expects ``{"type": "enabled", "budget_tokens": N}`` or
    # the field omitted entirely. We always pass ``max_tokens`` so the
    # answer body has room even when the thinking budget is non-zero
    # (max_tokens must be strictly greater than budget_tokens per spec).
    init_kwargs: dict[str, Any] = {
        "timeout": timeout_s if timeout_s is not None else get_model_timeout_s(),
        "max_retries": max_retries if max_retries is not None else get_model_max_retries(),
        "max_tokens": max_output_tokens
        if max_output_tokens is not None
        else get_max_output_tokens(),
    }
    if resolved_thinking > 0:
        init_kwargs["thinking"] = {"type": "enabled", "budget_tokens": resolved_thinking}
    return init_chat_model(model, **init_kwargs)


def build_budget_middlewares(
    *,
    model_call_limit: int | None = None,
    tool_call_limit: int | None = None,
) -> list[AgentMiddleware]:
    """Standard budget + convergence middleware stack for every OpenBot run.

    Four-layer defence against the runaway-loop failure mode (weaker
    models that say "let me check one more thing" until the budget
    runs out, then emit an empty answer):

    1. :class:`ToolCallRepetitionGuard` — short-circuits identical tool
       calls in a sliding window. Surgical: only stops *repeats*, lets
       genuinely-distinct exploration continue.
    2. :class:`ForceCommitBeforeBudget` — injects a "wrap up" directive
       at 70% of the LLM-call budget so the model has runway to commit
       its answer, instead of being killed mid-thought at 100%.
    3. ``ToolCallLimitMiddleware(exit_behavior="continue")`` — once
       ``thread_limit`` tool invocations are spent the next would-be
       tool call is intercepted and replaced with a synthetic tool
       message telling the model "tool budget exhausted, finish with
       what you have", giving the model one last LLM step to draft a
       final answer instead of being killed mid-flight.
    4. ``ModelCallLimitMiddleware(exit_behavior="end")`` — last-resort
       hard cap. Model middleware's "continue" variant short-circuits
       the graph immediately; we want a clean END so the LangGraph
       state still produces an ``AIMessage`` we can score.

    Layers 1-2 are the "harness constraints any model must obey"
    contribution — they impose convergence pressure that doesn't
    depend on the model's own self-discipline. A robust eval framework
    is supposed to work for *any* model; these two carry that load.
    Layers 3-4 remain as the hard backstop.

    Per-thread (= per Inspect sample) limits, never per-run accumulators.
    The middlewares maintain internal state (window deque, call counter)
    so a fresh agent must be built per sample — ``build_baseline_agent``
    already does this.
    """
    # Local import keeps the langchain-pull at this file's top out of
    # convergence_middleware's own import path → avoids a cycle.
    from evals.agents.convergence_middleware import build_convergence_middlewares

    resolved_model_limit = (
        model_call_limit if model_call_limit is not None else get_model_call_limit()
    )
    resolved_tool_limit = tool_call_limit if tool_call_limit is not None else get_tool_call_limit()

    middlewares: list[AgentMiddleware] = []
    middlewares.extend(build_convergence_middlewares(model_call_limit=resolved_model_limit))
    middlewares.extend(
        [
            ToolCallLimitMiddleware(
                thread_limit=resolved_tool_limit,
                exit_behavior="continue",
            ),
            ModelCallLimitMiddleware(
                thread_limit=resolved_model_limit,
                exit_behavior="end",
            ),
        ]
    )
    return middlewares


# HarnessProfile registration is global (keyed by model id) and idempotent
# under merge, but we still de-dupe to keep startup quiet.
_REGISTERED_PROFILES: set[str] = set()


def resolve_model(
    *,
    override: str | None = None,
    fallback: str | None = None,
) -> str:
    """Resolve a deepagents model id and normalise to ``provider:model`` form.

    Order: ``override`` → :data:`config.DEEPAGENTS_MODEL_ENV` →
    ``fallback`` → :attr:`DeepAgentsSettings.model
    <evals.common.config.DeepAgentsSettings.model>` default. Empty env
    vars are treated as unset (raw ``os.environ.get`` keeps the
    per-call override chain semantics that pydantic's snapshot doesn't
    cleanly express). A bare model name (no ``:``) is prefixed with
    ``anthropic:`` so langchain's ``init_chat_model`` routes through
    the Anthropic client (which honors ``ANTHROPIC_BASE_URL`` for
    self-hosted / proxy gateways).
    """
    effective_fallback = fallback if fallback is not None else config.DEEPAGENTS_MODEL_FALLBACK
    chosen = override or os.environ.get(config.DEEPAGENTS_MODEL_ENV) or effective_fallback
    if ":" not in chosen:
        chosen = f"anthropic:{chosen}"
    return chosen


def display_model_name(model: str) -> str:
    """Strip the LangChain ``provider:`` prefix for human-facing surfaces.

    Internally we keep the prefixed form (``anthropic:glm-4.5-air``) because
    :func:`langchain.chat_models.init_chat_model` uses it to pick the right
    SDK client. But the prefix is an implementation detail of how we route
    HTTP traffic — for LangSmith trace attributes, dashboards, and any other
    user-facing surface, the bare model name (``glm-4.5-air``) is what the
    reader actually wants to see. The prefix can be especially misleading
    when the provider label disagrees with the *physical* gateway (e.g.
    ``anthropic:`` while the call is going to GLM via an Anthropic-protocol-
    compatible endpoint at ``open.bigmodel.cn``).

    Idempotent: a string with no ``:`` is returned unchanged. Only the
    first segment is stripped, so an unusual id like ``openai:org:model``
    becomes ``org:model``.
    """
    if not isinstance(model, str) or ":" not in model:
        return model
    return model.split(":", 1)[1]


def _register_baseline_profile(model: str) -> None:
    """Register the OpenBot baseline ``HarnessProfile`` for ``model`` (idempotent).

    The previous baseline excluded ``write_todos`` (deepagents' built-in
    plan tool) on the rationale that single-issue eval tasks didn't need
    planning. In practice it bites the other way: weaker models (Haiku /
    GLM-air tier) loop on "let me check one more thing" precisely because
    they lack a structured place to track what they've already done.
    Enabling the plan tool gives the model a deterministic surface to
    commit interim observations, which empirically reduces stall loops
    on review / fix tasks. The harness-level convergence guards (see
    :mod:`evals.agents.convergence_middleware`) remain as the backstop.

    ``general_purpose_subagent.enabled=False`` is kept because eval cells
    are single-objective — the auto-attached delegation ``task`` tool
    only adds branching surface where the agent could lose its thread.
    """
    if model in _REGISTERED_PROFILES:
        return
    register_harness_profile(
        model,
        HarnessProfileConfig(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    _REGISTERED_PROFILES.add(model)


def build_baseline_agent(
    *,
    system_prompt: str,
    model: str,
    backend: BackendProtocol | None = None,
    tools: Sequence[BaseTool] = (),
    response_format: ResponseFormat = None,
    extra_middleware: Sequence[AgentMiddleware] = (),
    model_call_limit: int | None = None,
    tool_call_limit: int | None = None,
    timeout_s: int | None = None,
    max_retries: int | None = None,
    max_output_tokens: int | None = None,
    thinking_budget_tokens: int | None = None,
):
    """Construct a deepagents agent with the OpenBot-standard config applied.

    Args:
        system_prompt: Task-specific instructions. Placed at the front of the
            assembled system prompt; the registered HarnessProfile suffix
            (parallel-tool / tool-result-reflection guidance from the built-in
            sonnet-4-6 profile) follows.
        model: ``provider:model`` id. Use :func:`resolve_model` upstream if
            you want env-driven defaults.
        backend: Optional sandbox or filesystem backend. When provided,
            deepagents auto-attaches the ``ls`` / ``read_file`` / ``write_file``
            / ``edit_file`` / ``glob`` / ``grep`` / ``execute`` toolset
            (``execute`` only for sandbox-style backends).
        tools: Extra LangChain tools to merge with the built-in suite.
        response_format: Optional structured-output binding for the agent's
            terminal step. Accepts a Pydantic model, JSON schema dict, or a
            langchain ``AutoStrategy`` / ``ProviderStrategy`` / ``ToolStrategy``.
            When set, the compiled agent's output state carries a parsed
            ``structured_response`` field alongside the regular ``messages`` —
            so solvers can keep raw prose (e.g. for the safety scorer's
            canary scan) without losing schema enforcement.
        extra_middleware: Additional ``AgentMiddleware`` instances appended
            after the global budget guards. Per-task hooks (e.g. observability
            shims) can plug in here without re-implementing the budget stack.
        model_call_limit: Per-sample LLM-call cap. ``None`` → falls back to
            :func:`get_model_call_limit` (env-overridable global).
        tool_call_limit: Per-sample tool-invocation cap. ``None`` →
            :func:`get_tool_call_limit`.
        timeout_s: Per-request HTTP timeout in seconds. ``None`` →
            :func:`get_model_timeout_s` (env-overridable). Critical for not
            hanging on dead provider sockets.
        max_retries: HTTP-layer retry count for transient errors. ``None`` →
            :func:`get_model_max_retries`.
        max_output_tokens: Provider-side ``max_tokens`` per call. ``None`` →
            :func:`get_max_output_tokens` (env-overridable). Critical for
            thinking-style models that otherwise truncate before emitting
            their final answer.

    Returns:
        A compiled LangGraph ``CompiledStateGraph`` ready for
        ``invoke`` / ``ainvoke``.
    """
    _register_baseline_profile(model)
    middleware: list[AgentMiddleware] = build_budget_middlewares(
        model_call_limit=model_call_limit,
        tool_call_limit=tool_call_limit,
    )
    middleware.extend(extra_middleware)
    # Structured output is enforced *after* the agent loop by
    # :mod:`evals.agents.structured_finalizer`, not by binding
    # ``response_format`` into the deepagents graph. That decoupling
    # is what lets us keep extended thinking on for QA / review
    # without triggering the Anthropic ``tool_choice=forced +
    # thinking=enabled`` rejection (LangChain's shim drops
    # ``tool_choice`` in that combo, making the schema tool optional
    # and letting the model skip it mid-graph). The wrapper we
    # return here is duck-typed against ``CompiledStateGraph`` and
    # adds ``structured_response`` to the result dict on
    # ``ainvoke`` / ``invoke``.
    chat_model = build_chat_model(
        model,
        timeout_s=timeout_s,
        max_retries=max_retries,
        max_output_tokens=max_output_tokens,
        thinking_budget_tokens=thinking_budget_tokens,
    )
    agent = create_deep_agent(
        model=chat_model,
        backend=backend,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware,
    )
    if response_format is None:
        return agent
    schema = _resolve_response_schema(response_format)
    # Local import — structured_finalizer imports build_chat_model from
    # this module, so a top-level import would be circular.
    from evals.agents.structured_finalizer import wrap_agent_with_finalizer

    return wrap_agent_with_finalizer(agent, schema=schema, model=model)


def _resolve_response_schema(response_format: ResponseFormat) -> type[Any]:
    """Extract the Pydantic schema class from a ``response_format`` value.

    ``build_baseline_agent`` accepts the same union deepagents accepts
    (model class, LangChain strategy object, or JSON-schema dict). The
    finalizer's :func:`with_structured_output` call needs the raw
    Pydantic class. We unwrap the common shapes and reject anything we
    can't recover a class from — eval solvers should pass the model
    class directly.
    """
    if isinstance(response_format, type):
        return response_format
    if isinstance(response_format, AutoStrategy | ToolStrategy | ProviderStrategy):
        schema = getattr(response_format, "schema", None) or getattr(
            response_format, "response_format", None
        )
        if isinstance(schema, type):
            return schema
    raise TypeError(
        "build_baseline_agent now enforces structured output via "
        "structured_finalizer, which requires a Pydantic model class. "
        f"Got {type(response_format).__name__}; pass the model class "
        "directly (e.g. response_format=MySchema)."
    )


def build_run_config(
    *,
    sample_id: str,
    dataset_version: str,
    solver_family: str,
    model: str,
    git_sha: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
    recursion_limit: int | None = None,
) -> RunnableConfig:
    """Build a ``RunnableConfig`` for LangSmith trace naming + metadata.

    The ``run_name`` follows ``{dataset_version}/{sample_id}`` so that each
    sample's trace is identifiable at a glance in the LangSmith project view.
    Metadata is propagated to both the trace AND any LangSmith Experiment
    Run created by :class:`evals.agents.langsmith.LangSmithExperiment`.

    ``recursion_limit`` caps total LangGraph node transitions for the
    invocation. ``None`` → :func:`get_recursion_limit` (env-overridable).
    This is LangGraph's own backstop; the model/tool call middlewares
    should converge first, but if they don't this prevents an infinite
    graph loop from running forever.
    """
    # LangSmith / dashboard surface — record the bare model name without the
    # LangChain ``provider:`` routing prefix. See :func:`display_model_name`.
    metadata: dict[str, Any] = {
        "instance_id": sample_id,
        "dataset_version": dataset_version,
        "solver_family": solver_family,
        "model": display_model_name(model),
        "git_sha": git_sha or "unknown",
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return RunnableConfig(
        run_name=f"{dataset_version}/{sample_id}",
        metadata=metadata,
        recursion_limit=recursion_limit if recursion_limit is not None else get_recursion_limit(),
    )

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
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

# Type alias for the `response_format` knob — mirrors deepagents' signature.
ResponseFormat = (
    type[Any]
    | dict[str, Any]
    | AutoStrategy[Any]
    | ProviderStrategy[Any]
    | ToolStrategy[Any]
    | None
)

# Shared deepagents model selection. All eval solvers should resolve through
# this single env so one Doppler/config change moves the entire baseline.
_DEFAULT_ENV_VAR = "OPENBOT_DEEPAGENTS_MODEL"
_DEFAULT_FALLBACK = "anthropic:claude-sonnet-4-6"

# ─── Global runaway-guard budgets (per Inspect sample / per agent thread) ───
# Numbers chosen from observed smoke runs (review ≤5 model / ≤10 tool calls
# per sample; chat baseline 1-2 model calls; fix/test ~50 model / ~100 tool
# calls). Defaults sit comfortably above the typical envelope so we never
# truncate a healthy run, but cap pathological loops (~3-5x normal). Each
# is overridable via env var so a one-off campaign can dial it without code
# changes. ``exit_behavior="continue"`` (Tool) / ``"end"`` semantics matter:
# the tool middleware *injects a synthetic tool result asking the model to
# wrap up using current info* — i.e. the model gets a final turn to draft
# an answer instead of being externally cut. The model middleware similarly
# nudges termination after ``run_limit`` LLM calls. ``recursion_limit`` is
# LangGraph's hard backstop and only fires if both middlewares fail to
# converge.
_DEFAULT_MODEL_CALL_LIMIT = 20
_DEFAULT_TOOL_CALL_LIMIT = 40
_DEFAULT_RECURSION_LIMIT = 100


def _env_int(name: str, default: int) -> int:
    """Read a positive-int env var, fall back to ``default`` on missing / bad."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        return default


def get_model_call_limit() -> int:
    """Max LLM calls per Inspect sample (env: ``OPENBOT_DEEPAGENTS_MODEL_CALL_LIMIT``)."""
    return _env_int("OPENBOT_DEEPAGENTS_MODEL_CALL_LIMIT", _DEFAULT_MODEL_CALL_LIMIT)


def get_tool_call_limit() -> int:
    """Max tool invocations per Inspect sample (env: ``OPENBOT_DEEPAGENTS_TOOL_CALL_LIMIT``)."""
    return _env_int("OPENBOT_DEEPAGENTS_TOOL_CALL_LIMIT", _DEFAULT_TOOL_CALL_LIMIT)


def get_recursion_limit() -> int:
    """LangGraph hard step backstop (env: ``OPENBOT_DEEPAGENTS_RECURSION_LIMIT``).

    Set well above the tool / model limits because LangGraph counts every
    node transition (tool prep, tool result merge, etc.), not just LLM
    calls. Treat this as a last-resort guard; tuned middlewares should
    converge first.
    """
    return _env_int("OPENBOT_DEEPAGENTS_RECURSION_LIMIT", _DEFAULT_RECURSION_LIMIT)


def build_budget_middlewares(
    *,
    model_call_limit: int | None = None,
    tool_call_limit: int | None = None,
) -> list[AgentMiddleware]:
    """Standard budget middleware stack for every OpenBot deepagents run.

    Both middlewares use a graceful exit:
      * ``ToolCallLimitMiddleware(exit_behavior="continue")`` — once
        ``thread_limit`` tool invocations are spent the next would-be
        tool call is intercepted and replaced with a synthetic tool
        message telling the model "tool budget exhausted, finish with
        what you have", giving the model one last LLM step to draft a
        final answer instead of being killed mid-flight.
      * ``ModelCallLimitMiddleware(exit_behavior="end")`` — model
        middleware's "continue" variant short-circuits the graph
        immediately; we want a clean END so the LangGraph state still
        produces an ``AIMessage`` we can score. ``"end"`` halts the
        chain on the next iteration after the model returns, which is
        the closest analogue.

    Per-thread (= per Inspect sample) limits, never per-run accumulators.
    The same agent factory is reused across thousands of samples; we
    don't want sample N+1 to start already-empty.
    """
    return [
        ToolCallLimitMiddleware(
            thread_limit=tool_call_limit if tool_call_limit is not None else get_tool_call_limit(),
            exit_behavior="continue",
        ),
        ModelCallLimitMiddleware(
            thread_limit=model_call_limit
            if model_call_limit is not None
            else get_model_call_limit(),
            exit_behavior="end",
        ),
    ]


# HarnessProfile registration is global (keyed by model id) and idempotent
# under merge, but we still de-dupe to keep startup quiet.
_REGISTERED_PROFILES: set[str] = set()


def resolve_model(
    *,
    override: str | None = None,
    fallback: str = _DEFAULT_FALLBACK,
) -> str:
    """Resolve a deepagents model id and normalise to ``provider:model`` form.

    Order: ``override`` → ``$OPENBOT_DEEPAGENTS_MODEL`` → ``fallback``.
    Empty env vars are treated as unset. A bare model name (no ``:``) is
    prefixed with ``anthropic:`` so langchain's
    ``init_chat_model`` routes through the Anthropic client (which honors
    ``ANTHROPIC_BASE_URL`` for self-hosted / proxy gateways).
    """
    chosen = override or os.environ.get(_DEFAULT_ENV_VAR) or fallback
    if ":" not in chosen:
        chosen = f"anthropic:{chosen}"
    return chosen


def _register_baseline_profile(model: str) -> None:
    """Register the OpenBot baseline ``HarnessProfile`` for ``model`` (idempotent)."""
    if model in _REGISTERED_PROFILES:
        return
    register_harness_profile(
        model,
        HarnessProfileConfig(
            excluded_tools=frozenset({"write_todos"}),
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
    return create_deep_agent(
        model=model,
        backend=backend,
        tools=tools,
        system_prompt=system_prompt,
        response_format=response_format,
        middleware=middleware,
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
    Run created by :class:`evals.common.langsmith_experiments.LangSmithExperiment`.

    ``recursion_limit`` caps total LangGraph node transitions for the
    invocation. ``None`` → :func:`get_recursion_limit` (env-overridable).
    This is LangGraph's own backstop; the model/tool call middlewares
    should converge first, but if they don't this prevents an infinite
    graph loop from running forever.
    """
    metadata: dict[str, Any] = {
        "instance_id": sample_id,
        "dataset_version": dataset_version,
        "solver_family": solver_family,
        "model": model,
        "git_sha": git_sha or "unknown",
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return RunnableConfig(
        run_name=f"{dataset_version}/{sample_id}",
        metadata=metadata,
        recursion_limit=recursion_limit if recursion_limit is not None else get_recursion_limit(),
    )

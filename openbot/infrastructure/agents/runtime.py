# openbot/infrastructure/agents/runtime.py
"""BaseDeepAgentRuntime — shared DeepAgents execution engine."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from deepagents import create_deep_agent
from langchain_core.runnables import RunnableConfig

from openbot.infrastructure.agents._budget_middleware import (
    BudgetGuardState,
    _DeferredBudgetGuard,
    make_budget_guard,
)
from openbot.infrastructure.agents._middleware import ToolCallRepetitionGuard
from openbot.infrastructure.agents.model_names import display_name, normalize_for_langchain
from openbot.infrastructure.agents.profiles import (
    AgentBudgetExhaustedError,
    AgentExecutionError,
    AgentProfile,
    AgentRequest,
    AgentRunLimits,
    AgentSandboxForbiddenError,
    AgentSandboxRequiredError,
    AgentTimeoutError,
    DomainResult,
    SandboxRequirement,
)
from openbot.infrastructure.llm.model_router import primary_model_for
from openbot.infrastructure.observability import get_langfuse_handler, langfuse_agent_trace

if TYPE_CHECKING:
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import BaseMessage

_logger = logging.getLogger(__name__)

# Single budget-finalize prompt reused across profiles. Placed at module
# scope so it appears in tracing payloads under a stable name and stays
# easy to grep when tuning.
_BUDGET_FINALIZE_INSTRUCTION = (
    "BUDGET REACHED. Stop calling tools. Using ONLY the information already "
    "gathered in this conversation, emit one final response that conforms "
    "exactly to the required schema. If the work is incomplete, set the "
    "appropriate failure / partial fields in the schema (e.g. tests_passed=false, "
    "include the last test_output you saw, list any files you actually edited). "
    "Do not invent results you did not observe. Return the structured object now."
)

_REGISTERED_MODELS: set[str] = set()


def build_agent_chat_model(model: str, limits: AgentRunLimits) -> BaseChatModel:
    """Construct a LangChain chat model with explicit HTTP client configuration."""
    from langchain.chat_models import init_chat_model  # type: ignore[import]

    init_kwargs: dict[str, Any] = {}
    if limits.model_timeout_s is not None:
        init_kwargs["timeout"] = limits.model_timeout_s
    if limits.max_retries is not None:
        init_kwargs["max_retries"] = limits.max_retries
    if limits.max_output_tokens is not None:
        init_kwargs["max_tokens"] = limits.max_output_tokens
    if limits.thinking_budget_tokens > 0:
        init_kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": limits.thinking_budget_tokens,
        }
    return init_chat_model(model, **init_kwargs)


def _register_harness_profile(model: str) -> None:
    """Register HarnessProfile for model (idempotent). No-ops if API unavailable."""
    if model in _REGISTERED_MODELS:
        return
    # Optimistic write *before* the call so that a second concurrent
    # coroutine that passes the guard above (check-then-act window) will
    # skip the registration even if the first call hasn't returned yet.
    # The underlying deepagents.register_harness_profile is documented as
    # idempotent, but writing early is cheaper than relying on that.
    _REGISTERED_MODELS.add(model)
    try:
        from deepagents.profiles import (  # type: ignore[import]
            GeneralPurposeSubagentProfile,
            HarnessProfileConfig,
            register_harness_profile,
        )

        register_harness_profile(
            model,
            HarnessProfileConfig(
                general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            ),
        )
    except (ImportError, AttributeError):
        _logger.debug("HarnessProfile registration not available for model %s", model)


def _build_standard_middleware(limits: AgentRunLimits) -> list[AgentMiddleware]:
    """Standard safety middleware: repetition guard → tool cap → model cap.

    Both budget caps use *soft* termination paths so the agent can always
    emit one last structured response after the cap fires:

      - tool cap uses ``exit_behavior="continue"`` — blocks further tool
        calls but routes back to the model for a final answer.
      - model cap uses ``exit_behavior="end"`` — graph ends with a synthetic
        "limits exceeded" AIMessage. The runtime's outer ``run`` then runs a
        soft-finalize pass against the same chat model with
        ``with_structured_output`` to produce the schema-conforming reply.

    Profiles that depend on this asymmetry should size limits so the tool
    cap fires first (``model_call_limit > tool_call_limit``); the runtime's
    soft-finalize is the safety net for the residual case.
    """
    stack: list[Any] = [make_budget_guard(), ToolCallRepetitionGuard()]
    try:
        from langchain.agents.middleware import (  # type: ignore[import]
            ModelCallLimitMiddleware,
            ToolCallLimitMiddleware,
        )

        if limits.tool_call_limit is not None:
            stack.append(
                ToolCallLimitMiddleware(
                    thread_limit=limits.tool_call_limit,
                    # "end" terminates the graph immediately when the tool
                    # budget is exhausted, preventing the model from cycling
                    # through blocked-tool responses and hitting the LangGraph
                    # recursion limit prematurely (each router/tool node costs
                    # a recursion step, so "continue" burns the budget fast).
                    exit_behavior="end",
                )
            )
        if limits.model_call_limit is not None:
            stack.append(
                ModelCallLimitMiddleware(
                    thread_limit=limits.model_call_limit,
                    exit_behavior="end",
                )
            )
    except (ImportError, AttributeError):
        _logger.debug("ToolCallLimitMiddleware/ModelCallLimitMiddleware not available")
    return stack


def _resolve_per_task_cap(request: AgentRequest) -> Decimal:
    """Read the cap from request.metadata['per_task_cap_usd'], else default."""
    raw = request.metadata.get("per_task_cap_usd") if request.metadata else None
    if raw is None:
        return Decimal("1.50")
    if isinstance(raw, Decimal):
        return raw
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal("1.50")


def _validate_sandbox(profile: AgentProfile[Any], request: AgentRequest) -> None:
    req = profile.sandbox_requirement
    has_sandbox = request.sandbox is not None
    if req == SandboxRequirement.REQUIRED and not has_sandbox:
        raise AgentSandboxRequiredError(
            f"Profile '{profile.agent_name}' requires a sandbox but request.sandbox is None."
        )
    if req == SandboxRequirement.FORBIDDEN and has_sandbox:
        raise AgentSandboxForbiddenError(
            f"Profile '{profile.agent_name}' forbids a sandbox but request includes one."
        )


class BaseDeepAgentRuntime:
    """Shared DeepAgents execution engine."""

    async def run(
        self,
        profile: AgentProfile[DomainResult],
        request: AgentRequest,
    ) -> DomainResult:
        _validate_sandbox(profile, request)

        raw_model = primary_model_for(profile.feature)
        model = normalize_for_langchain(raw_model)
        _register_harness_profile(model)

        chat_model = build_agent_chat_model(model, profile.limits)

        middleware = _build_standard_middleware(profile.limits)
        middleware.extend(profile.extra_middleware)

        # Find the deferred budget guard (always at index 0) and rebind its
        # state to this run's task_id + cap + lookup. The lookup uses
        # ``CostMeterRepo.sum_recorded_for_task`` with a session per call so
        # we always read fresh.
        deferred = next(
            (m for m in middleware if isinstance(m, _DeferredBudgetGuard)),
            None,
        )
        if deferred is not None:
            cap = _resolve_per_task_cap(request)
            session_factory = request.metadata.get("session_factory")
            task_id = request.event.delivery_id or "<unknown>"

            async def _lookup() -> Decimal:
                if session_factory is None:
                    return Decimal("0")
                from openbot.infrastructure.persistence.repository import CostMeterRepo

                async with session_factory() as session:
                    return await CostMeterRepo(session).sum_recorded_for_task(task_id)

            budget_state = BudgetGuardState(
                task_id=task_id,
                cap_usd=cap,
                lookup=_lookup,
            )
            deferred.bind_state(budget_state)
        else:
            budget_state = None

        tools = list(profile.build_tools(request))

        effective_checkpointer = None
        if profile.checkpoint_enabled and request.run_id and request.checkpointer is not None:
            effective_checkpointer = request.checkpointer

        agent = create_deep_agent(
            model=chat_model,
            tools=tools,
            system_prompt=profile.system_prompt(request),
            response_format=profile.response_schema,
            middleware=middleware,
            checkpointer=effective_checkpointer,
        )

        config = RunnableConfig(
            recursion_limit=profile.limits.recursion_limit,
            metadata={
                "feature": profile.feature.value,
                "agent_profile": profile.agent_name,
                "run_id": request.run_id,
                "task_id": request.event.delivery_id,
                "repo": request.event.repo,
                "actor": request.event.actor,
                "model": display_name(model),
                "checkpoint_enabled": effective_checkpointer is not None,
                "sandbox_present": request.sandbox is not None,
                **dict(request.metadata),
            },
        )
        if effective_checkpointer is not None:
            config["configurable"] = {"thread_id": request.run_id}

        # Langfuse root trace: wraps the entire agent invocation so all
        # LangGraph sub-operations (LLM calls, tool calls, chain steps) nest
        # under ONE root "agent" span instead of scattering as 30-40 separate
        # root traces.  The CallbackHandler is created INSIDE the context so
        # it inherits the active OTel ContextVar set by start_as_current_observation.
        # No-op when LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set.
        trace_name = f"{profile.feature.value}-{profile.agent_name}"
        with langfuse_agent_trace(name=trace_name, session_id=request.run_id or ""):
            lf_callbacks = [h for h in [get_langfuse_handler()] if h is not None]
            if lf_callbacks:
                config["callbacks"] = lf_callbacks  # pyright: ignore[reportGeneralTypeIssues]

            invoke_coro = agent.ainvoke(
                {"messages": [{"role": "user", "content": profile.user_message(request)}]},
                config=config,
            )

            try:
                if profile.limits.wall_seconds is not None:
                    raw = await asyncio.wait_for(invoke_coro, timeout=profile.limits.wall_seconds)
                else:
                    raw = await invoke_coro
            except TimeoutError as exc:
                raise AgentTimeoutError(
                    f"Agent '{profile.agent_name}' exceeded wall_seconds={profile.limits.wall_seconds}"
                ) from exc
            except Exception as exc:
                exc_type_name = type(exc).__name__
                if "Termination" in exc_type_name or "Budget" in exc_type_name:
                    raise AgentBudgetExhaustedError(
                        f"Agent '{profile.agent_name}' budget exhausted"
                    ) from exc
                raise AgentExecutionError(
                    f"Agent '{profile.agent_name}' failed: {type(exc).__name__}"
                ) from exc

        # Surface the budget verdict so callers can flip partial=True.
        if budget_state is not None and budget_state.partial:
            # `RunnableConfig.metadata` is read-only after dispatch; we attach
            # to the request.metadata dict in place so the use case can read it.
            try:
                request.metadata["budget_partial"] = True
                request.metadata["budget_reason"] = budget_state.exceeded_reason or ""
            except TypeError:
                # request.metadata is a frozen mapping — log only.
                _logger.warning("budget_partial_metadata_unmutable")

        # Soft-finalize: if the profile expects a structured response but
        # middleware terminated the graph (e.g. ModelCallLimitMiddleware
        # exit_behavior="end" injects a synthetic AIMessage and skips the
        # response_format node), make ONE additional model call against
        # ``with_structured_output(schema)`` so we still return a valid
        # domain object instead of raising AgentStructuredOutputError.
        # This is bounded: exactly one extra call, no tools, no checkpoint.
        if profile.response_schema is not None and isinstance(raw, dict):
            structured = raw.get("structured_response")
            if structured is None:
                raw = await self._soft_finalize(
                    chat_model=chat_model,
                    schema=profile.response_schema,
                    raw=raw,
                    profile_name=profile.agent_name,
                )

        return profile.parse_result(raw)

    async def _soft_finalize(
        self,
        *,
        chat_model: BaseChatModel,
        schema: type[Any],
        raw: dict[str, Any],
        profile_name: str,
    ) -> dict[str, Any]:
        """Recover a missing structured_response by asking the model once
        more with the schema bound directly. No tools are exposed.

        Failure here is logged and re-raises through the existing
        AgentStructuredOutputError path in ``profile.parse_result``.
        """
        from langchain_core.messages import HumanMessage  # type: ignore[import]

        prior = raw.get("messages") or []
        finalize_input: list[BaseMessage] = [
            *prior,
            HumanMessage(content=_BUDGET_FINALIZE_INSTRUCTION),
        ]
        try:
            structured_model = chat_model.with_structured_output(schema)
            structured = await structured_model.ainvoke(finalize_input)
        except Exception as exc:  # pragma: no cover — exercised by integration
            _logger.warning(
                "soft_finalize_failed agent=%s err=%s", profile_name, type(exc).__name__
            )
            return raw
        _logger.info("soft_finalize_succeeded agent=%s", profile_name)
        return {**raw, "structured_response": structured}


__all__ = ["BaseDeepAgentRuntime", "build_agent_chat_model"]

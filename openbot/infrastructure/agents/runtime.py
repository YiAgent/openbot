# openbot/infrastructure/agents/runtime.py
"""BaseDeepAgentRuntime — shared DeepAgents execution engine."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from deepagents import create_deep_agent
from langchain_core.runnables import RunnableConfig

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
from openbot.infrastructure.observability import get_langfuse_handler

if TYPE_CHECKING:
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.language_models import BaseChatModel

_logger = logging.getLogger(__name__)

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
    """Standard safety middleware: repetition guard → tool cap → model cap."""
    stack: list[Any] = [ToolCallRepetitionGuard()]
    try:
        from langchain.agents.middleware import (  # type: ignore[import]
            ModelCallLimitMiddleware,
            ToolCallLimitMiddleware,
        )

        if limits.tool_call_limit is not None:
            stack.append(
                ToolCallLimitMiddleware(
                    thread_limit=limits.tool_call_limit,
                    # "continue" blocks exceeded tools but lets the model make
                    # one final call to produce the structured response.
                    # "end" is avoided because it terminates the graph at a
                    # tool-call node — before the model can emit the final
                    # structured output — causing AgentStructuredOutputError.
                    exit_behavior="continue",
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

        # Inject a fresh Langfuse callback so every agent run gets its own
        # trace with all steps + tool calls visible. No-op when
        # LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set.
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

        return profile.parse_result(raw)


__all__ = ["BaseDeepAgentRuntime", "build_agent_chat_model"]

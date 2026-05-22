# openbot/infrastructure/agents/profiles.py
"""AgentProfile protocol, request/limits data classes, and runtime error types.

This module defines the seam between the shared runtime (runtime.py) and
workflow-specific task agents (ReviewProfile, FixProfile, ChatProfile).

Key types:
  AgentRunLimits       — frozen limits for one agent invocation
  AgentRequest         — per-invocation inputs and context
  SandboxRequirement   — REQUIRED | OPTIONAL | FORBIDDEN
  AgentProfile         — Protocol[DomainResult] implemented by each profile
  Agent*Error          — stable infrastructure exceptions; use cases translate these
                         to user-facing comments
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from openbot.domain.workflows import Feature

if TYPE_CHECKING:
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.tools import BaseTool
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from pydantic import BaseModel

    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.domain.events import UnifiedEvent

from enum import StrEnum

DomainResult = TypeVar("DomainResult", covariant=True)


@dataclass(frozen=True, slots=True)
class AgentRunLimits:
    """Execution limits for a single agent invocation.

    All fields are optional except recursion_limit. None means "use the
    runtime default / don't configure this knob."
    """

    recursion_limit: int
    # Middleware-level caps
    model_call_limit: int | None = None
    tool_call_limit: int | None = None
    # Hard wall-clock ceiling (asyncio.wait_for)
    wall_seconds: int | None = None
    # HTTP client knobs — passed to build_agent_chat_model
    model_timeout_s: int | None = None
    max_retries: int | None = None
    max_output_tokens: int | None = None
    thinking_budget_tokens: int = 0  # 0 = disabled


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """All inputs and context for a single agent invocation.

    input:    Profile-specific keys documented in the spec.
              Review:  {"diff": str}
              Fix:     {"issue_title": str, "issue_body": str, "base_sha": str}
              Chat:    {"user_request": str}
    metadata: Extra key/value pairs merged into RunnableConfig metadata.
    """

    event: UnifiedEvent
    input: Mapping[str, Any]
    adapter: ChannelAdapterPort | None = None
    sandbox: SandboxPort | None = None
    run_id: str | None = None
    checkpointer: BaseCheckpointSaver | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SandboxRequirement(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    FORBIDDEN = "forbidden"


class AgentProfile(Protocol[DomainResult]):
    """Contract every task profile must satisfy.

    agent_name:          Stable string used in LangSmith trace names and logs.
    response_schema:     Pydantic BaseModel subclass for structured output,
                         or None for free-text profiles (chat).
    limits:              Per-profile execution and HTTP limits.
    sandbox_requirement: Whether the profile requires, allows, or forbids a sandbox.
    checkpoint_enabled:  True = profile SUPPORTS checkpointing when run_id +
                         checkpointer are both present in the request.
    extra_middleware:    Per-profile observability shims appended after the
                         standard safety stack. Must not replace or reorder it.
    """

    feature: Feature
    agent_name: str
    response_schema: type[BaseModel] | None
    limits: AgentRunLimits
    sandbox_requirement: SandboxRequirement
    checkpoint_enabled: bool
    extra_middleware: Sequence[AgentMiddleware]

    def system_prompt(self, request: AgentRequest) -> str: ...
    def user_message(self, request: AgentRequest) -> str: ...
    def build_tools(self, request: AgentRequest) -> Sequence[BaseTool]: ...
    def parse_result(self, result: Mapping[str, Any]) -> DomainResult: ...


# ── Runtime error hierarchy ──────────────────────────────────────────────────
# All errors are infrastructure-level. Use cases translate them to user-facing
# fallback comments. Never expose raw LangChain/DeepAgents text to GitHub.


class AgentError(RuntimeError):
    """Base for all runtime-level agent errors."""


class AgentSandboxRequiredError(AgentError):
    """Profile requires a sandbox but request.sandbox is None."""


class AgentSandboxForbiddenError(AgentError):
    """Profile forbids a sandbox but request includes one."""


class AgentStructuredOutputError(AgentError):
    """parse_result could not find or coerce the structured response."""


class AgentBudgetExhaustedError(AgentError):
    """Agent terminated via middleware budget limit before producing a result."""


class AgentTimeoutError(AgentError):
    """asyncio.wait_for fired on the wall_seconds limit."""


class AgentExecutionError(AgentError):
    """DeepAgents/LangGraph raised before producing any result."""


__all__ = [
    "AgentBudgetExhaustedError",
    "AgentError",
    "AgentExecutionError",
    "AgentProfile",
    "AgentRequest",
    "AgentRunLimits",
    "AgentSandboxForbiddenError",
    "AgentSandboxRequiredError",
    "AgentStructuredOutputError",
    "AgentTimeoutError",
    "DomainResult",
    "SandboxRequirement",
]

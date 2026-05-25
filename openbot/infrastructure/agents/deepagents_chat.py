# openbot/infrastructure/agents/deepagents_chat.py
"""Chat profile and compatibility wrapper — migrated to BaseDeepAgentRuntime.

The previous implementation passed `config or None` to ainvoke, meaning
LangGraph received no recursion_limit. The runtime always sets it from
profile.limits.recursion_limit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool

from openbot.domain.workflows import Feature
from openbot.infrastructure.agents._chat_tools import make_chat_tools
from openbot.infrastructure.agents.profiles import (
    AgentRequest,
    AgentRunLimits,
    SandboxRequirement,
)
from openbot.infrastructure.agents.runtime import BaseDeepAgentRuntime

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from openbot.domain.events import UnifiedEvent

_SYSTEM_PROMPT = """You are OpenBot, a GitHub maintainer bot assistant.

You are answering a GitHub comment mention inside an automation workflow.

You have three read-only tools:

  - `read_file(path)`: read a repo file (≤8 KB, truncated with `[truncated 8KB cap]`)
  - `grep_repo(pattern, path_glob=None, max_matches=20)`: pattern search
  - `list_files(path='.')`: list paths up to 4 levels deep, ≤200 entries

Rules:
- Use these tools to ground answers in actual repo content. Do not fabricate file contents.
- If output ends with `[truncated 8KB cap]`, say "(truncated)" in your reply rather than pretending you saw the rest.
- You CANNOT open PRs, push branches, label issues, edit files, run shell commands, or fetch URLs.
- If the user asks for any state-changing action ("open a PR", "push this", "merge"), refuse with a single line that points them to: assign the issue to @openbot to trigger fix, or open a PR for review.
- Be concise. One paragraph beats three bullet points unless the user asked for a list.
"""

_CHAT_LIMITS = AgentRunLimits(
    # 100 mirrors the review profile headroom. Each LangGraph node (model
    # call, router, tool dispatch, tool result) costs one recursion step.
    # GLM-5.1 can take more internal steps than the previous model, so 30
    # was too tight and triggered GraphRecursionError before model_call_limit
    # could fire cleanly.
    recursion_limit=100,
    # deepagents makes 3-5 internal model calls (planning + reasoning +
    # final response). The old limit of 3 triggered ModelCallLimitMiddleware
    # exit_behavior="end" with a synthetic "Model call limits exceeded" reply
    # before the useful response was produced. 10 gives the framework room
    # while still bounding runaway loops.
    model_call_limit=10,
    model_timeout_s=60,
    max_output_tokens=4_096,
)


def _target_label(event: UnifiedEvent) -> str:
    if event.issue_number is not None:
        return f"issue #{event.issue_number}"
    if event.pr_number is not None:
        return f"pull request #{event.pr_number}"
    return "GitHub thread"


def _coerce_text_part(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        if part.get("type") == "text":
            text = part.get("text")
            return text if isinstance(text, str) else ""
        return ""
    text_attr = getattr(part, "text", None)
    return text_attr if isinstance(text_attr, str) else ""


def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(_coerce_text_part(part) for part in content).strip()
    text_attr = getattr(content, "text", None)
    if isinstance(text_attr, str):
        return text_attr.strip()
    return ""


@dataclass
class ChatProfile:
    """Profile for the chat/mention reply agent."""

    feature: Feature = field(default=Feature.CHAT, init=False)
    agent_name: str = field(default="chat", init=False)
    response_schema: type[Any] | None = field(default=None, init=False)
    limits: AgentRunLimits = field(default_factory=lambda: _CHAT_LIMITS, init=False)
    sandbox_requirement: SandboxRequirement = field(
        default=SandboxRequirement.FORBIDDEN, init=False
    )
    checkpoint_enabled: bool = field(default=False, init=False)
    extra_middleware: Sequence[Any] = field(default_factory=list, init=False)

    def system_prompt(self, request: AgentRequest) -> str:
        return _SYSTEM_PROMPT

    def user_message(self, request: AgentRequest) -> str:
        event = request.event
        user_request = str(request.input.get("user_request", ""))
        return (
            "GitHub context:\n"
            f"- repository: {event.repo}\n"
            f"- target: {_target_label(event)}\n"
            f"- actor: {event.actor}\n"
            f"- event kind: {event.kind.value}\n\n"
            "User request:\n"
            f"{user_request}"
        )

    def build_tools(self, request: AgentRequest) -> Sequence[BaseTool]:
        adapter = getattr(request, "event_adapter", None)
        if adapter is None:
            # No adapter wired (e.g. test that built AgentRequest without
            # one) — fall back to no tools so the chat agent still answers
            # from prompt context. Production wiring always passes ctx.adapter.
            return []
        return list(make_chat_tools(adapter=adapter, event=request.event))

    def parse_result(self, result: Mapping[str, Any]) -> str:
        messages = result.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("deepagents_result_missing_messages")
        content = getattr(messages[-1], "content", None)
        reply = _extract_message_text(content)
        if not reply:
            raise ValueError("deepagents_result_missing_text")
        return reply


class DeepAgentsChatResponder:
    """Compatibility wrapper — delegates to BaseDeepAgentRuntime."""

    def __init__(self, runtime: BaseDeepAgentRuntime | None = None) -> None:
        self._runtime = runtime or BaseDeepAgentRuntime()

    async def reply_for_event(
        self,
        event: UnifiedEvent,
        *,
        user_request: str,
        run_id: str | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        adapter: Any | None = None,
        per_task_cap_usd: Decimal,
        session_factory: Any,
    ) -> str:
        return await self._runtime.run(
            ChatProfile(),
            AgentRequest(
                event=event,
                run_id=run_id,
                checkpointer=checkpointer,
                input={"user_request": user_request},
                event_adapter=adapter,
                metadata={
                    "per_task_cap_usd": per_task_cap_usd,
                    "session_factory": session_factory,
                },
            ),
        )


__all__ = ["ChatProfile", "DeepAgentsChatResponder"]

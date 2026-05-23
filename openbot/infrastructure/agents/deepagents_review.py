# openbot/infrastructure/agents/deepagents_review.py
"""PR review profile and compatibility wrapper — migrated to BaseDeepAgentRuntime.

The public class ``DeepAgentsReviewResponder`` keeps its original signature
so use cases need no changes. Internally it delegates to BaseDeepAgentRuntime.

ReviewProfile owns: system_prompt, user_message, tools, response_schema, parser.
The runtime owns: model construction, middleware, checkpoint wiring, telemetry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool

from openbot.domain.review import ReviewFindings
from openbot.domain.workflows import Feature
from openbot.infrastructure.agents._review_schema import (
    ReviewFindingsSchema,
    parse_structured_response,
)
from openbot.infrastructure.agents._review_tools import make_review_tools
from openbot.infrastructure.agents.profiles import (
    AgentRequest,
    AgentRunLimits,
    AgentStructuredOutputError,
    SandboxRequirement,
)
from openbot.infrastructure.agents.runtime import BaseDeepAgentRuntime

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.domain.events import UnifiedEvent

_MAX_DIFF_CHARS = 64_000


def _truncate_diff(diff: str) -> str:
    if len(diff) <= _MAX_DIFF_CHARS:
        return diff
    head = diff[:_MAX_DIFF_CHARS]
    dropped = len(diff) - _MAX_DIFF_CHARS
    return (
        f"{head}\n\n(diff truncated — dropped {dropped} chars; review remaining changes manually)"
    )


_SYSTEM_PROMPT = """You are OpenBot, a senior code reviewer. You will return a JSON object \
matching the provided schema — never plain text.

Your job:
- Read the provided unified diff and identify concrete, actionable issues.
- Focus on correctness, security, and obvious bugs first; style notes only if material.
- Severities: `critical` (data loss / security), `high` (likely bug), `medium` (smelly / risky), \
`low` (could be better), `nit` (taste). When in doubt, pick lower.
- If the diff is empty, trivial, or you find no issues: return `summary` only with `findings: []`.
- For each finding: set `file` to the repo-relative path. Set `line` to the line in the new file \
when the issue is local to one line; omit `line` for repo-wide findings (e.g. missing changelog).
- `quote` is optional — include a short source snippet (≤ 200 chars) when it helps the author \
locate the issue without clicking.

Tools available (use sparingly — total tool calls are budget-capped):
- `read_file(path)` — fetch the UTF-8 text of a file in the repo.
- `grep_repo(pattern, path_glob=None)` — find lines matching `pattern` across the repo.

Rules:
- Default to the inline diff. Only reach for tools when the diff alone is genuinely insufficient.
- Return ONE structured object. Do not emit chains of thought, multi-turn dialogue, or markdown \
prose outside the schema.
- Keep `summary` to one line.
- Do not invent line numbers — when you're not sure, omit `line` and explain in `message`.
"""

_REVIEW_LIMITS = AgentRunLimits(
    recursion_limit=25,
    tool_call_limit=8,  # slightly above ToolBudget=5 during transition; reduced in Task 5.1
    model_call_limit=10,
    model_timeout_s=120,
    max_retries=2,
    max_output_tokens=16_384,
)


@dataclass
class ReviewProfile:
    """Profile for the PR review agent."""

    feature: Feature = field(default=Feature.REVIEW, init=False)
    agent_name: str = field(default="review", init=False)
    response_schema: type[Any] | None = field(default=ReviewFindingsSchema, init=False)
    limits: AgentRunLimits = field(default_factory=lambda: _REVIEW_LIMITS, init=False)
    sandbox_requirement: SandboxRequirement = field(default=SandboxRequirement.OPTIONAL, init=False)
    checkpoint_enabled: bool = field(default=True, init=False)
    extra_middleware: Sequence[Any] = field(default_factory=list, init=False)

    def system_prompt(self, request: AgentRequest) -> str:
        return _SYSTEM_PROMPT

    def user_message(self, request: AgentRequest) -> str:
        diff = str(request.input.get("diff", ""))
        diff_block = (
            _truncate_diff(diff)
            if diff
            else "(diff unavailable — the PR may be closed, empty, or deleted)"
        )
        event = request.event
        return (
            "GitHub context:\n"
            f"- repository: {event.repo}\n"
            f"- pull request: #{event.pr_number}\n"
            f"- actor: {event.actor}\n\n"
            "Unified diff:\n"
            "```diff\n"
            f"{diff_block}\n"
            "```\n\n"
            "Review the diff and return a single structured object matching the schema."
        )

    def build_tools(self, request: AgentRequest) -> Sequence[BaseTool]:
        if request.adapter is None:
            return []
        return make_review_tools(adapter=request.adapter, event=request.event)

    def parse_result(self, result: Mapping[str, Any]) -> ReviewFindings:
        structured = result.get("structured_response")
        if structured is None:
            raise AgentStructuredOutputError("deepagents_result_missing_structured_response")
        return parse_structured_response(structured)


class DeepAgentsReviewResponder:
    """Compatibility wrapper — delegates to BaseDeepAgentRuntime."""

    def __init__(self, runtime: BaseDeepAgentRuntime | None = None) -> None:
        self._runtime = runtime or BaseDeepAgentRuntime()

    async def review_for_event(
        self,
        event: UnifiedEvent,
        *,
        adapter: ChannelAdapterPort,
        run_id: str | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> ReviewFindings:
        if event.pr_number is None:
            raise ValueError("deepagents_review_requires_pr_number")
        diff = await adapter.get_pr_diff(event, event.pr_number)
        return await self._runtime.run(
            ReviewProfile(),
            AgentRequest(
                event=event,
                adapter=adapter,
                run_id=run_id,
                checkpointer=checkpointer,
                input={"diff": diff},
            ),
        )


__all__ = ["DeepAgentsReviewResponder", "ReviewProfile"]

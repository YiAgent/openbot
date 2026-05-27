# openbot/infrastructure/agents/deepagents_fix.py
"""Fix profile and compatibility wrapper — migrated to BaseDeepAgentRuntime.

The public class ``DeepAgentsFixResponder`` keeps its original signature
so use cases need no changes. Internally it delegates to BaseDeepAgentRuntime.

FixProfile owns: system_prompt, user_message, tools, response_schema, parser.
The runtime owns: model construction, middleware, checkpoint wiring, telemetry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool

from openbot.application.ports.sandbox import SandboxPort
from openbot.domain.fix import FixOutcome
from openbot.domain.workflows import Feature
from openbot.infrastructure.agents._fix_schema import (
    FixOutcomeSchema,
    parse_structured_response,
)
from openbot.infrastructure.agents.profiles import (
    AgentRequest,
    AgentRunLimits,
    AgentSandboxRequiredError,
    AgentStructuredOutputError,
    SandboxRequirement,
)
from openbot.infrastructure.agents.runtime import BaseDeepAgentRuntime

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.domain.events import UnifiedEvent

_SYSTEM_PROMPT = """You are OpenBot, a senior engineer. You will fix the bug \
described in the GitHub issue below by editing files in the sandbox and \
running tests until they pass. Return a JSON object matching the schema — \
never plain text.

Workflow:
- Read the issue carefully. Form a hypothesis about which file(s) are wrong.
- Use `list_files` and `search_files` to navigate; use `read_file` to inspect.
- Use `write_file` to apply the smallest possible change that fixes the bug.
- Use `run_command` to run the project's test suite. You pick the command \
(e.g. `pytest -q`, `npm test`, `go test ./...`) based on what you see in the repo.
- If tests fail, iterate: re-read code, refine the fix, re-run tests.
- When tests pass, use `git_diff` to capture the final diff and return your structured answer.

Tools available (total tool calls are budget-capped — stop iterating before you exhaust):
- `read_file(path)` — read a UTF-8 file from the sandbox working tree.
- `write_file(path, content)` — overwrite or create a file. Always read first when modifying.
- `list_files(path=".")` — list a directory's entries (non-recursive).
- `run_command(command)` — run a shell command in the sandbox. Use this for tests and inspections.
- `git_diff()` — return `git diff` against the base commit. Call this once near the end.
- `search_files(pattern, path_glob="**/*")` — recursive grep (regex) in the working tree.

Rules:
- Make the smallest change that fixes the bug. Do not refactor unrelated code.
- Tests must pass on the final attempt — set `tests_passed=false` only if you \
genuinely could not fix it within your budget.
- Use the project's existing test runner. If you cannot detect one, default to \
`pytest -q` for Python repos.
- Return ONE structured object. Do not emit chains of thought, multi-turn \
dialogue, or markdown prose outside the schema.
- Keep `summary` to one line.
- `files_changed` lists the repo-relative paths you wrote.
- `test_output` should be the tail of the final test run (truncate yourself \
to ~2000 chars; the use case will truncate further if needed for GitHub).
"""

# recursion_limit=200: fix loops with 20 tool calls visit ~50 LangGraph nodes
# (ToolCall + ToolMessage + alternating LLM nodes). GLM-5.1 takes more internal
# steps than the previous model, so 60 triggers GraphRecursionError. 200 gives
# ample headroom for the larger tool budget while model_call_limit=20 still
# bounds runaway loops before the wall_seconds ceiling fires.
# tool_call_limit=20: matches retired ToolBudget value; enforced by middleware.
_FIX_LIMITS = AgentRunLimits(
    recursion_limit=200,
    tool_call_limit=20,
    model_call_limit=20,
    wall_seconds=1800,  # 30-minute hard ceiling for fix loops
    model_timeout_s=300,
    max_retries=2,
    max_output_tokens=16_384,
)


@dataclass
class FixProfile:
    """Profile for the issue-fix agent."""

    feature: Feature = field(default=Feature.FIX, init=False)
    agent_name: str = field(default="fix", init=False)
    response_schema: type[Any] | None = field(default=FixOutcomeSchema, init=False)
    limits: AgentRunLimits = field(default_factory=lambda: _FIX_LIMITS, init=False)
    sandbox_requirement: SandboxRequirement = field(default=SandboxRequirement.REQUIRED, init=False)
    checkpoint_enabled: bool = field(default=True, init=False)
    extra_middleware: Sequence[Any] = field(default_factory=list, init=False)

    def system_prompt(self, request: AgentRequest) -> str:
        return _SYSTEM_PROMPT

    def user_message(self, request: AgentRequest) -> str:
        event = request.event
        body = str(request.input.get("issue_body", "")).strip() or "(no description provided)"
        return (
            "GitHub context:\n"
            f"- repository: {event.repo}\n"
            f"- issue: #{event.issue_number}\n"
            f"- actor: {event.actor}\n"
            f"- base commit: {request.input.get('base_sha', '')}\n\n"
            f"Issue title: {request.input.get('issue_title', '')}\n\n"
            "Issue body:\n"
            f"{body}\n\n"
            "Fix the bug. Run the project's tests until they pass. Return one "
            "structured object matching the schema."
        )

    def build_tools(self, request: AgentRequest) -> Sequence[BaseTool]:
        # sandbox_requirement=REQUIRED so the runtime already raised
        # AgentSandboxRequiredError if sandbox is None. Cast is safe here.
        sandbox = request.sandbox
        if sandbox is None:  # pragma: no cover — runtime guard fires first
            raise AgentSandboxRequiredError(
                "FixProfile.build_tools requires a SandboxPort in request.sandbox"
            )
        from openbot.infrastructure.agents._fix_tools import make_fix_tools

        return make_fix_tools(sandbox=sandbox, event=request.event)

    def parse_result(self, result: Mapping[str, Any]) -> FixOutcome:
        structured = result.get("structured_response")
        if structured is None:
            raise AgentStructuredOutputError("deepagents_fix_result_missing_structured_response")
        return parse_structured_response(structured)


class DeepAgentsFixResponder:
    """Compatibility wrapper — delegates to BaseDeepAgentRuntime.

    Tools close over ``(sandbox, event)`` so the agent is rebuilt per call inside
    the runtime. Caching by model alone would let a previous tenant's sandbox
    handle leak into the next event — a multi-tenant correctness bug.
    """

    def __init__(self, runtime: BaseDeepAgentRuntime | None = None) -> None:
        self._runtime = runtime or BaseDeepAgentRuntime()

    async def fix_for_event(
        self,
        event: UnifiedEvent,
        *,
        adapter: ChannelAdapterPort,
        sandbox: SandboxPort,
        issue: dict[str, Any],
        run_id: str | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        per_task_cap_usd: Decimal = Decimal("1.50"),
        session_factory: Any = None,
    ) -> FixOutcome:
        """Run the fix loop and return a domain outcome.

        ``adapter`` is accepted for signature symmetry with
        ``DeepAgentsReviewResponder`` and to leave room for future
        in-loop GitHub calls (e.g. fetching related issues). Today the
        responder does not call it; the use case does the I/O.

        ``issue`` is the dict shape returned by
        ``ChannelAdapterPort.get_issue`` (see C.4). The responder reads
        ``title``, ``body``, and ``base_sha`` only; extra keys are
        ignored so the dict can grow without breaking this signature.
        """
        if event.issue_number is None:
            raise ValueError("deepagents_fix_requires_issue_number")
        return await self._runtime.run(
            FixProfile(),
            AgentRequest(
                event=event,
                adapter=adapter,
                sandbox=sandbox,
                run_id=run_id,
                checkpointer=checkpointer,
                input={
                    "issue_title": str(issue.get("title", "")),
                    "issue_body": str(issue.get("body", "")),
                    "base_sha": str(issue.get("base_sha", "")),
                },
                metadata={
                    "per_task_cap_usd": per_task_cap_usd,
                    "session_factory": session_factory,
                },
            ),
        )


__all__ = ["DeepAgentsFixResponder", "FixProfile"]

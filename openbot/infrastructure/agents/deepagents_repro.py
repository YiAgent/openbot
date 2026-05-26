# openbot/infrastructure/agents/deepagents_repro.py
"""Reproduce profile and compatibility wrapper — slice R2.

The reproduce loop runs *inside* the triage workflow (``Feature.TRIAGE``).
It is a separate ``AgentProfile`` rather than a branch in ``FixProfile``
because the budget, tool surface, and prompt all diverge from fix:

  - Test-authoring tool set (``write_file`` / ``git_diff`` / ``search_files``
    / ``read_file`` / ``list_files`` / ``run_command``).  The agent writes a
    test file, runs it to confirm the failure, then emits ``git_diff`` output
    as ``repro_artifact``.
  - Hard ``wall_seconds=180`` ceiling (fix runs up to an hour).
  - Prompt instructs the agent to early-exit with ``INSUFFICIENT_INFO``
    when the issue body is unusable — preventing it from burning the
    full budget on a malformed report.

``DeepAgentsReproResponder.reproduce_for_event(...)`` mirrors
``DeepAgentsFixResponder.fix_for_event(...)`` so callers can swap them
without learning a new signature. Internally both delegate to
``BaseDeepAgentRuntime.run``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool

from openbot.application.ports.sandbox import SandboxPort
from openbot.domain.repro import ReproOutcome
from openbot.domain.workflows import Feature
from openbot.infrastructure.agents._repro_schema import (
    ReproOutcomeSchema,
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

# ──────────────────────────────────────────────────────────────────────────────
# Prompt
# ──────────────────────────────────────────────────────────────────────────────

# Style choices that matter:
#   * No mention of write_file / fixing / apply-patch / refactoring — those are
#     fix-loop concepts and would tempt the model to call forbidden tools.
#   * Early-exit is the FIRST rule. Spec §3.4 caps this profile at
#     model_call_limit=15; a 5-turn exploration of "is the body usable" would
#     burn a third of the budget, so we want the agent to bail in turn 1.
#   * No `git_diff` mention — there's nothing to diff because there's nothing
#     to write.
_SYSTEM_PROMPT = """You are OpenBot's reproduce sub-agent. Your job is to \
**write a test that reproduces a reported bug** inside a sandboxed clone of \
the repository and return a structured outcome. You MUST end your run by \
returning a JSON object matching the schema — never plain text.

What you are doing (and not doing):
- You are NOT fixing the bug. Do not edit production code.
- You ARE writing a new test file (pytest-style) that triggers the reported \
failure, running it to confirm it fails, then capturing the diff.
- The test you write must FAIL on the current (buggy) codebase — that is the \
signal. A passing test means you did not reproduce the bug.

Early-exit policy (CRITICAL — read first):
- If the issue description lacks usable reproduction info (no clear failing \
input, no stacktrace, no expected/actual delta), return \
``status="insufficient_info"`` IMMEDIATELY in your first response. Do NOT \
explore the repo first. Use ``hypothesis`` to list the specific missing pieces \
(e.g. "no failing command", "no expected output").
- ``status="insufficient_info"`` outcomes MUST set ``command=null`` and \
``repro_artifact=null``. The schema enforces this; do not invent placeholders.

Workflow when reproduction info IS present:
1. Read the issue. Identify the expected behaviour and the failure.
2. Use ``list_files`` / ``search_files`` to understand the project layout and \
   find the right test directory and import paths.
3. Use ``read_file`` on the relevant source file(s) to confirm the import path.
4. Use ``write_file`` to create a new test file (e.g. ``tests/test_repro_<N>.py``) \
   containing one or more pytest test functions that trigger the reported failure.
5. Use ``run_command`` to run the test: ``["python", "-m", "pytest", \
   "tests/test_repro_<N>.py", "-v"]``.
   - Test FAILS (non-zero exit, output shows the expected error) → \
``status="reproduced"``. Set ``command`` to the pytest invocation.
   - Test PASSES unexpectedly → refine the test and retry once. If still \
passing, return ``status="not_reproduced"`` with an explanation.
6. Call ``git_diff`` to capture the unified diff of your test file.
   Set ``repro_artifact`` to the full output of ``git_diff``. \
   Set ``repro_artifact_filename`` to the test file path you wrote.

Tools available:
- ``read_file(path)`` — read a UTF-8 file from the workspace.
- ``write_file(path, content)`` — write a file (test only, not production code).
- ``list_files(path=".", max=200)`` — list directory entries.
- ``search_files(pattern, path_glob=None)`` — grep across the workspace.
- ``run_command(command, timeout_seconds=60)`` — run an argv-list command. \
Returns ``{stdout, stderr, exit_code, timed_out}``.
- ``git_diff()`` — return the unified diff of all changes in the workspace.

Budget discipline:
- You have a strict per-turn budget. Skip exploratory chains \
when one targeted ``read_file`` would do.
- When you have made ~70% of your apparent tool-call budget, STOP and return \
the best ``ReproOutcome`` you have so far.
- ``output_excerpt`` must be the failing test output's tail. Truncate yourself \
to ~2000 chars; the schema will further truncate if you overshoot.

Rules:
- Return ONE structured object. No prose, no markdown outside the schema.
- Keep ``summary`` to one comment-friendly line.
- ``hypothesis`` is your root-cause guess (REPRODUCED / NOT_REPRODUCED paths) \
OR the list of missing info (INSUFFICIENT_INFO path).
- Never invent test output. If you didn't run the test, leave \
``output_excerpt=""`` and pick a non-REPRODUCED status.
"""


# ──────────────────────────────────────────────────────────────────────────────
# Limits — spec §3.4 (do not edit without updating the spec)
# ──────────────────────────────────────────────────────────────────────────────

# Reproduce limits are deliberately tight compared to fix. The agent is doing
# one of two things — bail on insufficient info, or run a single reproduction
# command + maybe one variation. Neither needs 80 tool calls.
#
#   * recursion_limit=40       — ~3 LangGraph nodes per tool call x ~12 calls
#     + middleware overhead. Leaves slack without inviting runaway loops.
#   * tool_call_limit=30       — generous enough for "list_files → read_file
#     → run_command → maybe rerun with -v" plus a handful of misfires.
#   * model_call_limit=15      — > tool_call_limit so the tool cap fires
#     first via exit_behavior="continue" (same discipline as fix).
#   * wall_seconds=180         — hard ceiling. Reproduce should not take
#     longer than 3 minutes; fix can take an hour, but fix's payoff is a PR.
#   * max_output_tokens=4000   — comment-shaped output. The schema enforces
#     a 2000-char excerpt budget so the model can't dump multi-KB blobs.
#   * thinking_budget_tokens=0 — extended thinking is off; the prompt's
#     workflow rules give the model enough structure without it.
_REPRO_LIMITS = AgentRunLimits(
    recursion_limit=40,
    model_call_limit=15,
    tool_call_limit=30,
    wall_seconds=180,
    max_output_tokens=4000,
    thinking_budget_tokens=0,
)


# ──────────────────────────────────────────────────────────────────────────────
# Profile
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class ReproProfile:
    """Profile for the issue-reproduce agent.

    ``feature = Feature.TRIAGE`` because reproduce is a sub-step of the
    triage workflow — the audit row's ``workflow`` column reads
    ``triage`` while ``outcome`` carries ``reproduce:<status>``.
    """

    feature: Feature = field(default=Feature.TRIAGE, init=False)
    agent_name: str = field(default="repro", init=False)
    response_schema: type[Any] | None = field(default=ReproOutcomeSchema, init=False)
    limits: AgentRunLimits = field(default_factory=lambda: _REPRO_LIMITS, init=False)
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
            "Reproduce the bug. Do NOT attempt to fix it. Return one "
            "structured ReproOutcome matching the schema."
        )

    def build_tools(self, request: AgentRequest) -> Sequence[BaseTool]:
        # sandbox_requirement=REQUIRED is checked at the runtime layer too,
        # but a direct caller (e.g. an eval harness) could bypass that path.
        # Re-asserting here keeps the contract self-contained.
        sandbox = request.sandbox
        if sandbox is None:
            raise AgentSandboxRequiredError(
                "ReproProfile.build_tools requires a SandboxPort in request.sandbox"
            )
        from openbot.infrastructure.agents._repro_tools import make_repro_tools

        return make_repro_tools(sandbox=sandbox, event=request.event)

    def parse_result(self, result: Mapping[str, Any]) -> ReproOutcome:
        structured = result.get("structured_response")
        if structured is None:
            raise AgentStructuredOutputError("deepagents_repro_result_missing_structured_response")
        return parse_structured_response(structured)


# ──────────────────────────────────────────────────────────────────────────────
# Responder (compatibility wrapper)
# ──────────────────────────────────────────────────────────────────────────────


class DeepAgentsReproResponder:
    """Thin wrapper around ``BaseDeepAgentRuntime`` — mirrors
    ``DeepAgentsFixResponder``.

    Tools close over ``(sandbox, event)`` so the agent is rebuilt per
    call inside the runtime. Caching by model alone would let a previous
    tenant's sandbox handle leak into the next event.
    """

    def __init__(self, runtime: BaseDeepAgentRuntime | None = None) -> None:
        self._runtime = runtime or BaseDeepAgentRuntime()

    async def reproduce_for_event(
        self,
        event: UnifiedEvent,
        *,
        adapter: ChannelAdapterPort,
        sandbox: SandboxPort,
        issue: dict[str, Any],
        run_id: str | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> ReproOutcome:
        """Run the reproduce loop and return a domain outcome.

        ``adapter`` is accepted for signature symmetry with the other
        responders; today the use case owns all GitHub I/O so the
        responder does not call it.

        ``issue`` is the dict shape returned by
        ``ChannelAdapterPort.get_issue``. The responder reads
        ``title`` / ``body`` / ``base_sha`` only — extra keys are
        ignored so the dict can grow without breaking this signature.
        """
        if event.issue_number is None:
            raise ValueError("deepagents_repro_requires_issue_number")
        return await self._runtime.run(
            ReproProfile(),
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
            ),
        )


__all__ = ["DeepAgentsReproResponder", "ReproProfile"]

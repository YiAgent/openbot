"""DeepAgent-backed fix responder — slice C.

What it owns:

  - Build a per-event DeepAgent with tools that close over a live
    sandbox handle (see C.6 — ``make_fix_tools``).
  - Pass the issue body to the model and read back the structured
    answer via ``response_format=FixOutcomeSchema``.
  - Convert the pydantic object to a domain ``FixOutcome`` and return
    it. Nothing else.

What it does NOT own:

  - GitHub PR creation, branch creation, push (use case).
  - Sandbox lifecycle (the use case creates and closes the sandbox).
  - Issue fetching (the use case fetches via the channel adapter).
  - Comment templating for failures (the use case owns the templates).

This separation matches ``deepagents_review.py``: the responder is a
pure ``UnifiedEvent + side-effecting tools → FixOutcome`` function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deepagents import create_deep_agent

from openbot.application.ports.sandbox import SandboxPort
from openbot.domain.events import UnifiedEvent
from openbot.domain.fix import FixOutcome
from openbot.infrastructure.agents._fix_schema import (
    FixOutcomeSchema,
    parse_structured_response,
)
from openbot.infrastructure.agents._fix_tools import make_fix_tools
from openbot.infrastructure.llm.model_router import Feature, primary_model_for

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from openbot.application.ports.channel_adapter import ChannelAdapterPort

# Same value used by the review responder. LangGraph counts every node
# visit; a fix loop with 20 tool calls visits ~50 nodes (20 ToolCall +
# 20 ToolMessage + alternating LLM nodes), so the budget is the real cap.
# 25 here is a defensive ceiling against unbounded reasoning loops the
# tool-side budget can't see.
_RECURSION_LIMIT = 25

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


def _normalize_model_name(model: str) -> str:
    """Map ``provider/name`` (LiteLLM) → ``provider:name`` (langchain_litellm).

    Same helper as ``deepagents_review.py``. Duplicated rather than
    shared because the rule is small and keeping responders independent
    helps when one needs to evolve faster than the other.
    """
    if ":" in model:
        return model
    if "/" in model:
        provider, name = model.split("/", 1)
        return f"{provider}:{name}"
    return model


def _user_prompt(
    event: UnifiedEvent,
    *,
    issue_title: str,
    issue_body: str,
    base_sha: str,
) -> str:
    """Format the user-turn prompt for the fix agent.

    Issue title and body are passed as plain text — the agent reads them
    to form a hypothesis. ``base_sha`` is included so the agent has a
    stable reference point for ``git_diff`` and so tool calls that
    reference commits can ground themselves.
    """
    body = issue_body.strip() or "(no description provided)"
    return (
        "GitHub context:\n"
        f"- repository: {event.repo}\n"
        f"- issue: #{event.issue_number}\n"
        f"- actor: {event.actor}\n"
        f"- base commit: {base_sha}\n\n"
        f"Issue title: {issue_title}\n\n"
        "Issue body:\n"
        f"{body}\n\n"
        "Fix the bug. Run the project's tests until they pass. Return one "
        "structured object matching the schema."
    )


def _extract_outcome(result: dict[str, Any]) -> FixOutcome:
    """Read DeepAgents' structured-output channel and coerce to the domain type.

    Mirrors ``_extract_findings`` in ``deepagents_review.py``: structured
    response is the contract; missing it is a programmer error, not a
    user error, so we raise rather than returning a default.
    """
    structured = result.get("structured_response")
    if structured is None:
        raise ValueError("deepagents_fix_result_missing_structured_response")
    return parse_structured_response(structured)


class DeepAgentsFixResponder:
    """Stateless fix responder — a fresh agent is built per call.

    Tools close over ``(sandbox, event)`` so the agent must be rebuilt
    per call. Caching by model alone would let a previous tenant's
    sandbox handle leak into the next event — a multi-tenant correctness
    bug, not a perf issue.
    """

    async def fix_for_event(
        self,
        event: UnifiedEvent,
        *,
        adapter: ChannelAdapterPort,
        sandbox: SandboxPort,
        issue: dict[str, Any],
        run_id: str | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
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
        del adapter  # unused — see docstring
        if event.issue_number is None:
            raise ValueError("deepagents_fix_requires_issue_number")
        tools = make_fix_tools(sandbox=sandbox, event=event)
        agent = create_deep_agent(
            model=_normalize_model_name(primary_model_for(Feature.FIX)),
            tools=tools,
            system_prompt=_SYSTEM_PROMPT,
            response_format=FixOutcomeSchema,
            checkpointer=checkpointer,
        )
        config: RunnableConfig = {"recursion_limit": _RECURSION_LIMIT}
        if run_id and checkpointer:
            config["configurable"] = {"thread_id": run_id}
        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": _user_prompt(
                            event,
                            issue_title=str(issue.get("title", "")),
                            issue_body=str(issue.get("body", "")),
                            base_sha=str(issue.get("base_sha", "")),
                        ),
                    }
                ]
            },
            config=config,
        )
        return _extract_outcome(result)


__all__ = ["DeepAgentsFixResponder"]

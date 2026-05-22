"""DeepAgent-backed PR review responder — slice B.

What changed from slice A2:

  - The agent now returns *structured findings* (one ``ReviewFindings``
    value) instead of a free-form markdown reply. We pass
    ``response_format=ReviewFindingsSchema`` so DeepAgents/LangGraph coerces
    the model's final answer into a typed pydantic object, then convert
    that to the domain ``ReviewFindings`` (anti-corruption boundary —
    pydantic stops at this module).
  - The use case (`maybe_run_review`) filters findings by severity and
    submits them through the PR Review API (POST .../reviews) instead of
    posting one free-form comment. This module no longer knows or cares
    how the result is rendered to GitHub.

Carried over from A2:

  - Tools (``read_file``, ``grep_repo``) close over ``(adapter, event)`` so
    the agent is rebuilt per call — caching by model alone would be a
    multi-tenant correctness bug.
  - ``ToolBudget`` caps total tool invocations (5); ``recursion_limit``
    (25) catches non-tool loops the budget guard can't see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deepagents import create_deep_agent

from openbot.domain.events import UnifiedEvent
from openbot.domain.review import ReviewFindings
from openbot.infrastructure.agents._review_schema import (
    ReviewFindingsSchema,
    parse_structured_response,
)
from openbot.infrastructure.agents._review_tools import make_review_tools
from openbot.infrastructure.llm.model_router import Feature, primary_model_for

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from openbot.application.ports.channel_adapter import ChannelAdapterPort

# PR diffs can grow large; opus-4-7 has plenty of headroom but pure-noise
# tokens (lockfile churn, generated assets) waste budget. 64KB ≈ ~16k
# tokens — still well below model context but keeps cost predictable.
_MAX_DIFF_CHARS = 64_000

# LangGraph counts every node visit toward this limit; a ReAct loop with
# 5 tool calls visits ~15-20 nodes. 25 gives the agent a small budget over
# the ToolBudget cap so it can still produce a final answer if the very
# last tool call exhausts the budget.
_RECURSION_LIMIT = 25

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
- `read_file(path)` — fetch the UTF-8 text of a file in the repo. Use when the diff references a \
function or class you need to see in full to judge the change.
- `grep_repo(pattern, path_glob=None)` — find lines matching `pattern` across the repo. \
`path_glob` is GitHub's `path:` qualifier (substring, not real glob).

Rules:
- Default to the inline diff. Only reach for tools when the diff alone is genuinely insufficient.
- Return ONE structured object. Do not emit chains of thought, multi-turn dialogue, or markdown \
prose outside the schema.
- Keep `summary` to one line.
- Do not invent line numbers — when you're not sure, omit `line` and explain in `message`.
"""


def _normalize_model_name(model: str) -> str:
    """Map ``provider/name`` (LiteLLM) → ``provider:name`` (langchain_litellm)."""
    if ":" in model:
        return model
    if "/" in model:
        provider, name = model.split("/", 1)
        return f"{provider}:{name}"
    return model


def _truncate_diff(diff: str) -> str:
    if len(diff) <= _MAX_DIFF_CHARS:
        return diff
    head = diff[:_MAX_DIFF_CHARS]
    dropped = len(diff) - _MAX_DIFF_CHARS
    return (
        f"{head}\n\n(diff truncated — dropped {dropped} chars; review remaining changes manually)"
    )


def _user_prompt(event: UnifiedEvent, diff: str) -> str:
    diff_block = (
        _truncate_diff(diff)
        if diff
        else "(diff unavailable — the PR may be closed, empty, or deleted)"
    )
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


def _extract_findings(result: dict[str, Any]) -> ReviewFindings:
    """Read DeepAgents' structured-output channel and coerce to the domain type.

    Some agent runtimes (langgraph + ``response_format``) put the parsed
    response under ``structured_response``; older or simpler graphs may
    just include it in the final message. We try the dedicated key first
    and fall back to inspecting the last message only if needed —
    ``parse_structured_response`` does the heavy lifting.
    """
    structured = result.get("structured_response")
    if structured is not None:
        return parse_structured_response(structured)
    raise ValueError("deepagents_result_missing_structured_response")


class DeepAgentsReviewResponder:
    """Stateless responder — a fresh agent is built per call.

    Why no cache: tools close over ``(adapter, event)``. Caching the
    compiled graph by model would let tools from a previous event leak
    into the current run. The cost of ``create_deep_agent`` is in-process
    object wiring, not network, so building per call is cheap relative to
    the LLM call itself.
    """

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
        tools = make_review_tools(adapter=adapter, event=event)
        # Only activate checkpointing when both pieces are present: a
        # checkpointer without a run_id has no thread_id to key on and
        # LangGraph would error. Gate both on the same condition so they
        # are always in sync.
        effective_checkpointer = checkpointer if (run_id and checkpointer) else None
        agent = create_deep_agent(
            model=_normalize_model_name(primary_model_for(Feature.REVIEW)),
            tools=tools,
            system_prompt=_SYSTEM_PROMPT,
            response_format=ReviewFindingsSchema,
            checkpointer=effective_checkpointer,
        )
        config: RunnableConfig = {"recursion_limit": _RECURSION_LIMIT}
        if run_id and effective_checkpointer:
            config["configurable"] = {"thread_id": run_id}
        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": _user_prompt(event, diff),
                    }
                ]
            },
            config=config,
        )
        return _extract_findings(result)


__all__ = ["DeepAgentsReviewResponder"]

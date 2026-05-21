"""DeepAgent-backed PR review responder — slice A2.

What changed from slice A:

  - The agent is now a *tool-using* ReAct loop. It still receives an inline
    diff in the user prompt (cheap context that's always there), but it can
    also call ``read_file`` to pull the rest of a file the diff touches and
    ``grep_repo`` to find related code.
  - The agent is rebuilt per event because tools close over
    ``(adapter, event)``. Caching by model alone (slice A's
    ``_agent_for_model``) is now a correctness bug, not just an optimization
    miss — tools from event A would leak into event B's run.
  - Runaway agent loops are bounded twice: ``ToolBudget`` (default 5 calls)
    sits at the port boundary so each tool call is counted; ``recursion_limit``
    on the langgraph config (25) catches non-tool loops the budget can't see.

What's still out of scope (slice B):

  - Structured findings (severity, file, line) via PR review API.
  - Multi-turn / "ask the author" follow-up.
  - Re-review on PR_SYNCHRONIZED (incremental review wiring lives in F3; the
    responder is event-stateless today, by design).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deepagents import create_deep_agent

from openbot.domain.events import UnifiedEvent
from openbot.infrastructure.agents._review_tools import make_review_tools
from openbot.infrastructure.llm.model_router import Feature, primary_model_for

if TYPE_CHECKING:
    from openbot.application.ports.channel_adapter import ChannelAdapterPort

# PR diffs can grow large; opus-4-7 has plenty of headroom but pure-noise
# tokens (lockfile churn, generated assets) waste budget. 64KB ≈ ~16k
# tokens — still well below model context but keeps cost predictable.
# Slice B will replace this with surgical diff slicing per-file.
_MAX_DIFF_CHARS = 64_000

# LangGraph counts every node visit toward this limit; a ReAct loop with
# 5 tool calls visits ~15-20 nodes. 25 gives the agent a small budget over
# the ToolBudget cap so it can still produce a final answer if the very
# last tool call exhausts the budget.
_RECURSION_LIMIT = 25

_SYSTEM_PROMPT = """You are OpenBot, a senior code reviewer responding inline on a GitHub pull request.

Your job:
- Read the provided unified diff and call out concrete, actionable issues.
- Focus on correctness, security, and obvious bugs first; style notes only if material.
- If the diff is empty or trivial (lockfile-only, docs-only, formatting), say so and approve briefly.
- Quote the relevant `path:line` when you flag something so the author can find it.

Tools available (use sparingly — total tool calls are budget-capped):
- `read_file(path)` — fetch the UTF-8 text of a file in the repo. Use this when the diff
  references a function or class you need to see in full to judge the change.
- `grep_repo(pattern, path_glob=None)` — find lines matching `pattern` across the repo.
  Useful for "is this function called elsewhere?" or "is there an existing helper for this?".
  `path_glob` is GitHub's `path:` qualifier — substring match, not a real glob.

Rules:
- Default to the inline diff. Only reach for tools when the diff alone is genuinely insufficient.
- One reply per call. No multi-step planning visible to the author — produce the final review.
- Markdown formatting is fine; keep the reply under ~600 words.
- Do not claim you ran code or executed tests. You can only read repo files and search code.
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
        "Review the diff and produce a single, complete PR comment."
    )


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


def _extract_reply(result: dict[str, Any]) -> str:
    messages = result.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("deepagents_result_missing_messages")
    content = getattr(messages[-1], "content", None)
    reply = _extract_message_text(content)
    if not reply:
        raise ValueError("deepagents_result_missing_text")
    return reply


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
    ) -> str:
        if event.pr_number is None:
            raise ValueError("deepagents_review_requires_pr_number")
        diff = await adapter.get_pr_diff(event, event.pr_number)
        tools = make_review_tools(adapter=adapter, event=event)
        agent = create_deep_agent(
            model=_normalize_model_name(primary_model_for(Feature.REVIEW)),
            tools=tools,
            system_prompt=_SYSTEM_PROMPT,
        )
        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": _user_prompt(event, diff),
                    }
                ]
            },
            config={"recursion_limit": _RECURSION_LIMIT},
        )
        return _extract_reply(result)


__all__ = ["DeepAgentsReviewResponder"]

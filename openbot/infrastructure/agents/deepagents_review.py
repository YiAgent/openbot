"""DeepAgent-backed PR review responder — slice A of the review workflow.

Scope (slice A, locked):
  - One inline diff in the prompt; no agent tools (``tools=[]`` like chat).
  - The agent produces one consolidated review reply; ``maybe_run_review``
    posts it as a single PR comment via the channel adapter.
  - No structured findings, no severity filter, no multi-turn — those are
    slice B/C in ``docs/superpowers/plans/2026-05-20-review-fix-deepagent.md``.

Why inline diff over LangChain tools (the Q1=B alternative): the
``ChannelAdapterPort`` only exposes ``get_pr_diff`` today; adding
``read_file`` / ``grep_repo`` to the port would have widened slice A
beyond what we want to ship this week. Slice A2 will introduce those
tools and switch to a tool-using agent.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

from deepagents import create_deep_agent

from openbot.domain.events import UnifiedEvent
from openbot.infrastructure.llm.model_router import Feature, primary_model_for

if TYPE_CHECKING:
    from openbot.application.ports.channel_adapter import ChannelAdapterPort

# PR diffs can grow large; opus-4-7 has plenty of headroom but pure-noise
# tokens (lockfile churn, generated assets) waste budget. 64KB ≈ ~16k
# tokens — still well below model context but keeps cost predictable.
# Slice B will replace this with surgical diff slicing per-file.
_MAX_DIFF_CHARS = 64_000

_SYSTEM_PROMPT = """You are OpenBot, a senior code reviewer responding inline on a GitHub pull request.

Your job:
- Read the provided unified diff and call out concrete, actionable issues.
- Focus on correctness, security, and obvious bugs first; style notes only if material.
- If the diff is empty or trivial (lockfile-only, docs-only, formatting), say so and approve briefly.
- Quote the relevant `path:line` when you flag something so the author can find it.

Rules:
- Use only the diff provided in the prompt — do not claim you ran code, fetched files, or inspected anything else.
- One reply per call. No multi-step planning, no questions back to the author — produce the final review.
- Markdown formatting is fine; keep the reply under ~600 words.
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


@lru_cache(maxsize=4)
def _agent_for_model(model: str):
    """Build once, reuse across PR events for the same model."""
    return create_deep_agent(
        model=_normalize_model_name(model),
        tools=[],
        system_prompt=_SYSTEM_PROMPT,
    )


class DeepAgentsReviewResponder:
    """Stateless responder — agents are cached per-model via ``_agent_for_model``."""

    async def review_for_event(
        self,
        event: UnifiedEvent,
        *,
        adapter: ChannelAdapterPort,
    ) -> str:
        if event.pr_number is None:
            raise ValueError("deepagents_review_requires_pr_number")
        diff = await adapter.get_pr_diff(event, event.pr_number)
        agent = _agent_for_model(primary_model_for(Feature.REVIEW))
        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": _user_prompt(event, diff),
                    }
                ]
            }
        )
        return _extract_reply(result)


__all__ = ["DeepAgentsReviewResponder"]

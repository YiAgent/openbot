from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deepagents import create_deep_agent

from openbot.domain.events import UnifiedEvent
from openbot.infrastructure.llm.model_router import Feature, primary_model_for
from openbot.infrastructure.observability import get_langfuse_handler

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import BaseCheckpointSaver

_SYSTEM_PROMPT = """You are OpenBot, a GitHub maintainer bot assistant.

You are answering a GitHub comment mention inside an automation workflow.

Rules:
- Answer the user's request directly and concisely.
- Use only the context provided in the prompt.
- Do not claim you inspected repository files, ran commands, or fetched remote data unless that context is explicitly provided.
- If the user asks for action you cannot complete from the provided context, say so clearly and suggest the next concrete step.
"""


def _normalize_model_name(model: str) -> str:
    if ":" in model:
        return model
    if "/" in model:
        provider, name = model.split("/", 1)
        return f"{provider}:{name}"
    return model


def _target_label(event: UnifiedEvent) -> str:
    if event.issue_number is not None:
        return f"issue #{event.issue_number}"
    if event.pr_number is not None:
        return f"pull request #{event.pr_number}"
    return "GitHub thread"


def _user_prompt(event: UnifiedEvent, user_request: str) -> str:
    return (
        "GitHub context:\n"
        f"- repository: {event.repo}\n"
        f"- target: {_target_label(event)}\n"
        f"- actor: {event.actor}\n"
        f"- event kind: {event.kind.value}\n\n"
        "User request:\n"
        f"{user_request}"
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


class DeepAgentsChatResponder:
    """Chat responder — a fresh agent is built per call.

    The ``lru_cache`` that previously cached agents by model was removed
    when checkpointer support landed: caching would let a caller with
    ``checkpointer=None`` receive a cached graph that was built without a
    checkpointer, silently skipping persistence. Rebuilding per call is
    cheap relative to the LLM invocation and avoids the correctness risk.
    """

    async def reply_for_event(
        self,
        event: UnifiedEvent,
        *,
        user_request: str,
        run_id: str | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> str:
        # Only activate checkpointing when both pieces are present: a
        # checkpointer without a run_id has no thread_id to key on and
        # LangGraph would error. Gate both on the same condition so they
        # are always in sync.
        effective_checkpointer = checkpointer if (run_id and checkpointer) else None
        agent = create_deep_agent(
            model=_normalize_model_name(primary_model_for(Feature.CHAT)),
            tools=[],
            system_prompt=_SYSTEM_PROMPT,
            checkpointer=effective_checkpointer,
        )
        config: RunnableConfig = {}
        if run_id and effective_checkpointer:
            config["configurable"] = {"thread_id": run_id}
        # Inject a fresh Langfuse callback so each chat run gets its own
        # trace with all agent steps visible. No-op when
        # LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set.
        lf_callbacks = [h for h in [get_langfuse_handler()] if h is not None]
        if lf_callbacks:
            config["callbacks"] = lf_callbacks  # pyright: ignore[reportGeneralTypeIssues]
        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": _user_prompt(event, user_request),
                    }
                ]
            },
            config=config or None,
        )
        return _extract_reply(result)


__all__ = ["DeepAgentsChatResponder"]

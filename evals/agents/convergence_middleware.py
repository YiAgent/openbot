"""Harness-level convergence guards — middleware that any model must respect.

A robust eval framework constrains agent behaviour through harness controls
rather than through model-specific prompts or model selection. The two
middlewares here implement that principle for the loop pattern observed
on GLM-4.5-air (and other Haiku-class models): an agent that keeps saying
"let me check one more thing" without committing to an answer, eventually
hitting the call-limit safeguards with no useful output.

Both middlewares are model-agnostic — they observe state shape (tool
invocation history, LLM call count) and inject deterministic steering, not
prompt-specific cues.

:class:`ToolCallRepetitionGuard`
    Counts identical tool invocations (same ``name`` + canonicalised
    ``args``) within a sliding window. When the same tool call shows up
    more than ``threshold`` times, the next attempt is short-circuited
    with a synthetic :class:`ToolMessage` telling the model the result
    won't change and to commit to an answer.

:class:`ForceCommitBeforeBudget`
    Before the agent exhausts its LLM-call budget, inject a directive
    user-role message asking the model to emit its final answer. The
    hint fires once, at ``commit_at_fraction`` * ``model_call_limit``,
    so the model has a clear "wrap up" signal before the hard cap
    kills it mid-thought.

Both are designed to fail soft: if the model hits the steering message
once and still doesn't commit, the underlying call-limit middlewares are
the backstop.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)


def _canonical_tool_signature(tool_call: dict[str, Any]) -> str:
    """Stable string for a tool invocation, used as a hash key.

    ``args`` is JSON-dumped with sorted keys so a re-invocation of
    ``read_file(path="/x.py")`` matches the previous one byte-for-byte
    even if the model emits the keys in a different order. We accept
    "logically identical args" as the equivalence class; semantic
    equivalence beyond key-order (e.g. glob vs grep that hit the same
    files) is out of scope — keep the guard simple and predictable.
    """
    name = tool_call.get("name") or ""
    args = tool_call.get("args") or {}
    try:
        args_json = json.dumps(args, sort_keys=True, ensure_ascii=True)
    except TypeError:
        args_json = repr(args)
    return f"{name}::{args_json}"


class ToolCallRepetitionGuard(AgentMiddleware):
    """Short-circuit a tool call that has been issued too many times.

    Maintains a per-agent-instance sliding window of the last ``window``
    tool calls. When a new invocation's signature matches the threshold
    inside that window, return a synthetic ``ToolMessage`` instead of
    actually invoking the tool. The model then sees a deterministic
    "you've already done this, commit your answer" instruction.

    This is the canonical defence against weaker models that loop on
    "let me search one more time" — they're forced to either reach a
    different conclusion or finalize. It does not depend on the model's
    own reasoning text and works identically across providers.

    Args:
        threshold: Number of times a tool call signature must appear in
            the window before the guard intercepts. Default 3 — first
            two repeats may be legitimate (e.g. retry after a partial
            read); the third is virtually always a stall pattern.
        window: How many recent tool calls are kept for matching. Default
            10 — large enough to catch alternating "search A / search B
            / search A / search B" loops, small enough that genuinely
            distinct phases of investigation don't trip the guard.
        max_steers: Cap on how many times a single sample can be
            steered. Default 2 — if the agent ignores 2 steering
            messages, the regular call-limit middlewares take over.
    """

    name = "ToolCallRepetitionGuard"

    _STEER_MESSAGE = (
        "REPEATED TOOL CALL DETECTED. You have already invoked this exact "
        "tool call {count} times in the recent window — the result will "
        "not change. Stop re-running and either:\n"
        "  1. Commit to the findings/answer you have based on what you've "
        "already observed, or\n"
        "  2. Try a meaningfully different tool/argument.\n"
        "Re-running the same call again wastes your budget."
    )

    def __init__(
        self,
        *,
        threshold: int = 3,
        window: int = 10,
        max_steers: int = 2,
    ) -> None:
        super().__init__()
        if threshold < 2:
            raise ValueError("threshold must be ≥ 2 (first repeat is often legit)")
        if window < threshold:
            raise ValueError("window must be ≥ threshold")
        self._threshold = threshold
        self._window: deque[str] = deque(maxlen=window)
        self._steers_remaining = max_steers

    def _intercept(self, request: ToolCallRequest) -> ToolMessage | None:
        """Return a synthetic ToolMessage if the call is a repeated stall.

        Counts ``signature`` occurrences (including the current one) in
        the window. If the count crosses ``threshold`` and we still have
        steering budget, emit the steer; otherwise let the call proceed
        and rely on the call-limit middlewares as backstop.
        """
        sig = _canonical_tool_signature(request.tool_call)
        prior_count = sum(1 for s in self._window if s == sig)
        # Record the current call regardless — even if we intercept, the
        # signature occurred and should count toward the window.
        self._window.append(sig)
        current_count = prior_count + 1
        if current_count < self._threshold:
            return None
        if self._steers_remaining <= 0:
            return None
        self._steers_remaining -= 1
        logger.info(
            "ToolCallRepetitionGuard intercepted repeated tool call "
            "(name=%s, count=%d/%d in last %d, steers_left=%d)",
            request.tool_call.get("name"),
            current_count,
            self._threshold,
            self._window.maxlen,
            self._steers_remaining,
        )
        return ToolMessage(
            content=self._STEER_MESSAGE.format(count=current_count),
            tool_call_id=request.tool_call.get("id", ""),
            name=request.tool_call.get("name", ""),
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        intercept = self._intercept(request)
        if intercept is not None:
            return intercept
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        intercept = self._intercept(request)
        if intercept is not None:
            return intercept
        return await handler(request)


class ForceCommitBeforeBudget(AgentMiddleware):
    """Inject a "wrap up" directive before the LLM-call budget runs out.

    The default ``ModelCallLimitMiddleware`` only acts *at* the budget
    cap, and the model's last actual call often ends mid-reasoning with
    a give-up answer (the symptom we saw: ``{"findings": []}``). This
    middleware fires once, at ``commit_at_fraction`` of the budget,
    telling the model "you have N model calls left; emit your final
    answer in the required format now". Gives the model a clear runway
    to commit rather than being killed mid-thought.

    Implemented as a ``before_model`` hook returning a state patch that
    appends a user-role message. We use ``HumanMessage`` rather than
    ``SystemMessage`` so the message lands inside the conversation flow
    where the model is paying attention, and so it stays compatible
    with providers that allow only one system message at the start.

    Args:
        model_call_limit: The same cap passed to
            :class:`ModelCallLimitMiddleware`. We don't try to discover
            it from sibling middlewares — explicit pass keeps the
            convergence math reproducible.
        commit_at_fraction: When ``llm_calls_made / model_call_limit``
            ≥ this fraction, fire the steering message. Default 0.7 —
            leaves the model ~30% of its budget to actually emit the
            final answer.
    """

    name = "ForceCommitBeforeBudget"

    _STEER_MESSAGE = (
        "BUDGET HEADS-UP: you have used {used}/{limit} of your model-call "
        "budget. Stop investigating and emit your final structured answer "
        "in the next response. Do not request more tool calls unless one "
        "is strictly necessary to commit the answer. If you are uncertain, "
        "report what you observed with the confidence level you have — an "
        "empty answer is worse than a partial one."
    )

    def __init__(
        self,
        *,
        model_call_limit: int,
        commit_at_fraction: float = 0.7,
    ) -> None:
        super().__init__()
        if model_call_limit <= 0:
            raise ValueError("model_call_limit must be > 0")
        if not 0 < commit_at_fraction < 1:
            raise ValueError("commit_at_fraction must be in (0, 1)")
        self._model_call_limit = model_call_limit
        self._commit_threshold = max(1, int(model_call_limit * commit_at_fraction))
        self._calls_made = 0
        self._steered = False

    def _maybe_steer_state(self) -> dict[str, Any] | None:
        """Return a state patch that appends the steering HumanMessage, once.

        Called from both ``before_model`` and ``awrap_model_call`` so the
        steering lands regardless of sync/async path. Idempotent —
        ``self._steered`` prevents double-injection on the same sample.
        """
        self._calls_made += 1
        if self._steered:
            return None
        if self._calls_made < self._commit_threshold:
            return None
        self._steered = True
        logger.info(
            "ForceCommitBeforeBudget injecting wrap-up directive (call %d/%d, threshold %d)",
            self._calls_made,
            self._model_call_limit,
            self._commit_threshold,
        )
        return {
            "messages": [
                HumanMessage(
                    content=self._STEER_MESSAGE.format(
                        used=self._calls_made,
                        limit=self._model_call_limit,
                    )
                )
            ]
        }

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._maybe_steer_state()

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._maybe_steer_state()


def build_convergence_middlewares(
    *,
    model_call_limit: int,
    repetition_threshold: int = 3,
    repetition_window: int = 10,
    repetition_max_steers: int = 2,
    commit_at_fraction: float = 0.7,
) -> list[AgentMiddleware]:
    """One-call builder for the standard convergence guard stack.

    Returns a list ordered so that:
      1. Repetition guard runs first per tool call — if a call is a
         repeated stall it gets short-circuited before reaching the
         tool layer.
      2. ForceCommit fires once on the way to a model call.

    Both are stateful (window deque + counters), so a fresh instance is
    needed per sample. ``build_baseline_agent`` already constructs the
    full middleware stack per ``ainvoke``, so the per-sample lifetime
    matches naturally.
    """
    return [
        ToolCallRepetitionGuard(
            threshold=repetition_threshold,
            window=repetition_window,
            max_steers=repetition_max_steers,
        ),
        ForceCommitBeforeBudget(
            model_call_limit=model_call_limit,
            commit_at_fraction=commit_at_fraction,
        ),
    ]


# AIMessage is referenced indirectly in tests; re-export so tests don't
# need a separate langchain import.
__all__ = [
    "AIMessage",
    "ForceCommitBeforeBudget",
    "ToolCallRepetitionGuard",
    "build_convergence_middlewares",
]

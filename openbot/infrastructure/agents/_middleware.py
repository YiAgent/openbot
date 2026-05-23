# openbot/infrastructure/agents/_middleware.py
"""Production-safe DeepAgents middleware.

This module provides the ToolCallRepetitionGuard for production use.
It does NOT import from evals.* — it is a standalone production implementation.
The convergence logic mirrors evals/agents/middleware.py but has no eval
dependency.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.messages.tool import ToolCall

logger = logging.getLogger(__name__)


def _canonical_tool_signature(tool_call: ToolCall) -> str:
    name = tool_call.get("name") or ""
    args = tool_call.get("args") or {}
    try:
        args_json = json.dumps(args, sort_keys=True, ensure_ascii=True)
    except TypeError:
        args_json = repr(args)
    return f"{name}::{args_json}"


class ToolCallRepetitionGuard(AgentMiddleware):
    """Short-circuit a tool call issued too many times in a sliding window.

    Args:
        threshold:  Times a signature must appear before interception. Default 3.
        window:     How many recent calls to keep. Default 10.
        max_steers: Max interceptions per run. Default 2.
    """

    # name is inherited from AgentMiddleware as a property returning __class__.__name__
    # which already equals "ToolCallRepetitionGuard" — no override needed.

    _STEER_MESSAGE = (
        "REPEATED TOOL CALL DETECTED. You have already invoked this exact "
        "tool call {count} times in the recent window — the result will "
        "not change. Stop re-running and either:\n"
        "  1. Commit to the findings/answer you have based on what you've "
        "already observed, or\n"
        "  2. Try a meaningfully different tool/argument.\n"
        "Re-running the same call again wastes your budget."
    )

    def __init__(self, *, threshold: int = 3, window: int = 10, max_steers: int = 2) -> None:
        super().__init__()
        if threshold < 2:
            raise ValueError("threshold must be ≥ 2")
        if window < threshold:
            raise ValueError("window must be ≥ threshold")
        self._threshold = threshold
        self._window: deque[str] = deque(maxlen=window)
        self._steers_remaining = max_steers

    def _intercept(self, request: ToolCallRequest) -> ToolMessage | None:
        sig = _canonical_tool_signature(request.tool_call)
        prior_count = sum(1 for s in self._window if s == sig)
        self._window.append(sig)
        current_count = prior_count + 1
        if current_count < self._threshold or self._steers_remaining <= 0:
            return None
        self._steers_remaining -= 1
        logger.info(
            "ToolCallRepetitionGuard intercepted name=%s count=%d/%d steers_left=%d",
            request.tool_call.get("name"),
            current_count,
            self._threshold,
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
        intercepted = self._intercept(request)
        return intercepted if intercepted is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        intercepted = self._intercept(request)
        return intercepted if intercepted is not None else await handler(request)


__all__ = ["ToolCallRepetitionGuard"]

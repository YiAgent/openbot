# DeepAgents Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate the three bespoke DeepAgents responders (review/fix/chat) behind a single `BaseDeepAgentRuntime` with proper HTTP client configuration, middleware stack, and typed profiles.

**Architecture:** A new `runtime.py` owns `create_deep_agent` construction, HTTP client setup, middleware wiring, checkpoint activation, and structured output parsing. Each workflow provides an `AgentProfile` (a frozen dataclass implementing the Protocol) that declares its system prompt, tools, limits, and result parser. Compatibility wrappers on the existing responder classes delegate to the runtime, keeping the public API stable throughout migration.

**Tech Stack:** deepagents ≥ 0.6.1, langchain (AgentMiddleware, ModelCallLimitMiddleware, ToolCallLimitMiddleware, init_chat_model), LangGraph (BaseCheckpointSaver), Python 3.12, pytest-asyncio.

---

## File Map

| Path | Status | Purpose |
|---|---|---|
| `openbot/infrastructure/agents/model_names.py` | **CREATE** | `normalize_for_langchain()` and `display_name()` — two distinct transforms |
| `openbot/infrastructure/agents/profiles.py` | **CREATE** | `AgentRunLimits`, `AgentRequest`, `SandboxRequirement`, `AgentProfile` Protocol, error classes |
| `openbot/infrastructure/agents/_middleware.py` | **CREATE** | Production-safe `ToolCallRepetitionGuard` (no evals imports) |
| `openbot/infrastructure/agents/runtime.py` | **CREATE** | `build_agent_chat_model`, `BaseDeepAgentRuntime` |
| `tests/infrastructure/agents/test_model_names.py` | **CREATE** | Unit tests for model_names |
| `tests/infrastructure/agents/test_runtime.py` | **CREATE** | Unit tests for runtime (monkeypatched create_deep_agent) |
| `openbot/infrastructure/agents/deepagents_review.py` | **SHRINK** | Add `ReviewProfile`; wrapper delegates to runtime |
| `openbot/infrastructure/agents/deepagents_fix.py` | **SHRINK** | Add `FixProfile`; wrapper delegates to runtime |
| `openbot/infrastructure/agents/deepagents_chat.py` | **SHRINK** | Add `ChatProfile`; wrapper delegates to runtime |
| `openbot/application/use_cases/review.py` | **PATCH** | Pass `run_id` + `checkpointer` down to responder |
| `openbot/infrastructure/agents/_review_tools.py` | **PATCH** | Phase 5: retire `ToolBudget` |
| `openbot/infrastructure/agents/_fix_tools.py` | **PATCH** | Phase 5: retire `ToolBudget` |
| `openbot/infrastructure/agents/__init__.py` | **PATCH** | Re-export `BaseDeepAgentRuntime`, `AgentProfile`, `AgentRequest` |

---

## Task 1.1: model_names.py

**Files:**
- Create: `openbot/infrastructure/agents/model_names.py`
- Create: `tests/infrastructure/agents/test_model_names.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/infrastructure/agents/test_model_names.py
from openbot.infrastructure.agents.model_names import display_name, normalize_for_langchain


def test_normalize_slash_to_colon() -> None:
    assert normalize_for_langchain("anthropic/GLM-5.1") == "anthropic:GLM-5.1"


def test_normalize_already_colon_idempotent() -> None:
    assert normalize_for_langchain("anthropic:GLM-5.1") == "anthropic:GLM-5.1"


def test_normalize_bare_name_unchanged() -> None:
    assert normalize_for_langchain("GLM-5.1") == "GLM-5.1"


def test_normalize_openai_slash() -> None:
    assert normalize_for_langchain("openai/gpt-5-mini") == "openai:gpt-5-mini"


def test_display_name_strips_prefix() -> None:
    assert display_name("anthropic:GLM-5.1") == "GLM-5.1"


def test_display_name_bare_idempotent() -> None:
    assert display_name("GLM-5.1") == "GLM-5.1"


def test_display_name_strips_only_first_segment() -> None:
    # openai:org:model → org:model
    assert display_name("openai:org:model") == "org:model"
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
uv run pytest tests/infrastructure/agents/test_model_names.py -v
```

Expected: `ModuleNotFoundError: No module named 'openbot.infrastructure.agents.model_names'`

- [ ] **Step 3: Implement model_names.py**

```python
# openbot/infrastructure/agents/model_names.py
"""Model name helpers for the DeepAgents runtime.

Two distinct transforms:
  normalize_for_langchain — LiteLLM "provider/name" → LangChain "provider:name"
  display_name            — strip "provider:" prefix for logs and LangSmith traces

They are kept separate because they serve different callers with different
intent: routing vs. human-readable display.
"""

from __future__ import annotations


def normalize_for_langchain(model: str) -> str:
    """Map provider/name (LiteLLM) → provider:name (langchain).

    anthropic/GLM-5.1 → anthropic:GLM-5.1
    anthropic:GLM-5.1 → anthropic:GLM-5.1  (idempotent)
    GLM-5.1           → GLM-5.1            (bare names untouched)
    """
    if ":" in model:
        return model
    if "/" in model:
        provider, name = model.split("/", 1)
        return f"{provider}:{name}"
    return model


def display_name(model: str) -> str:
    """Strip the provider: prefix for human-facing surfaces (logs, LangSmith).

    anthropic:GLM-5.1 → GLM-5.1
    GLM-5.1           → GLM-5.1        (idempotent)
    openai:org:model  → org:model      (only the first segment is stripped)
    """
    if ":" not in model:
        return model
    return model.split(":", 1)[1]


__all__ = ["display_name", "normalize_for_langchain"]
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
uv run pytest tests/infrastructure/agents/test_model_names.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add openbot/infrastructure/agents/model_names.py tests/infrastructure/agents/test_model_names.py
git commit -m "feat(runtime): add model_names with normalize_for_langchain + display_name"
```

---

## Task 1.2: profiles.py

**Files:**
- Create: `openbot/infrastructure/agents/profiles.py`

No tests in this task — `profiles.py` is pure data types tested via runtime tests in Task 1.3.

- [ ] **Step 1: Implement profiles.py**

```python
# openbot/infrastructure/agents/profiles.py
"""AgentProfile protocol, request/limits data classes, and runtime error types.

This module defines the seam between the shared runtime (runtime.py) and
workflow-specific task agents (ReviewProfile, FixProfile, ChatProfile).

Key types:
  AgentRunLimits       — frozen limits for one agent invocation
  AgentRequest         — per-invocation inputs and context
  SandboxRequirement   — REQUIRED | OPTIONAL | FORBIDDEN
  AgentProfile         — Protocol[DomainResult] implemented by each profile
  Agent*Error          — stable infrastructure exceptions; use cases translate these
                         to user-facing comments
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from openbot.domain.workflows import Feature

if TYPE_CHECKING:
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.tools import BaseTool
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from pydantic import BaseModel

    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.domain.events import UnifiedEvent

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass


DomainResult = TypeVar("DomainResult")


@dataclass(frozen=True, slots=True)
class AgentRunLimits:
    """Execution limits for a single agent invocation.

    All fields are optional except recursion_limit. None means "use the
    runtime default / don't configure this knob."
    """

    recursion_limit: int
    # Middleware-level caps
    model_call_limit: int | None = None
    tool_call_limit: int | None = None
    # Hard wall-clock ceiling (asyncio.wait_for)
    wall_seconds: int | None = None
    # HTTP client knobs — passed to build_agent_chat_model
    model_timeout_s: int | None = None
    max_retries: int | None = None
    max_output_tokens: int | None = None
    thinking_budget_tokens: int = 0  # 0 = disabled


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """All inputs and context for a single agent invocation.

    input:    Profile-specific keys documented in the spec.
              Review:  {"diff": str}
              Fix:     {"issue_title": str, "issue_body": str, "base_sha": str}
              Chat:    {"user_request": str}
    metadata: Extra key/value pairs merged into RunnableConfig metadata.
    """

    event: "UnifiedEvent"
    input: Mapping[str, Any]
    adapter: "ChannelAdapterPort | None" = None
    sandbox: "SandboxPort | None" = None
    run_id: str | None = None
    checkpointer: "BaseCheckpointSaver | None" = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SandboxRequirement(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    FORBIDDEN = "forbidden"


class AgentProfile(Protocol[DomainResult]):
    """Contract every task profile must satisfy.

    agent_name:          Stable string used in LangSmith trace names and logs.
    response_schema:     Pydantic BaseModel subclass for structured output,
                         or None for free-text profiles (chat).
    limits:              Per-profile execution and HTTP limits.
    sandbox_requirement: Whether the profile requires, allows, or forbids a sandbox.
    checkpoint_enabled:  True = profile SUPPORTS checkpointing when run_id +
                         checkpointer are both present in the request.
    extra_middleware:    Per-profile observability shims appended after the
                         standard safety stack. Must not replace or reorder it.
    """

    feature: Feature
    agent_name: str
    response_schema: "type[BaseModel] | None"
    limits: AgentRunLimits
    sandbox_requirement: SandboxRequirement
    checkpoint_enabled: bool
    extra_middleware: "Sequence[AgentMiddleware]"

    def system_prompt(self, request: AgentRequest) -> str: ...
    def user_message(self, request: AgentRequest) -> str: ...
    def build_tools(self, request: AgentRequest) -> "Sequence[BaseTool]": ...
    def parse_result(self, result: Mapping[str, Any]) -> DomainResult: ...


# ── Runtime error hierarchy ──────────────────────────────────────────────────
# All errors are infrastructure-level. Use cases translate them to user-facing
# fallback comments. Never expose raw LangChain/DeepAgents text to GitHub.


class AgentError(RuntimeError):
    """Base for all runtime-level agent errors."""


class AgentSandboxRequiredError(AgentError):
    """Profile requires a sandbox but request.sandbox is None."""


class AgentSandboxForbiddenError(AgentError):
    """Profile forbids a sandbox but request includes one."""


class AgentStructuredOutputError(AgentError):
    """parse_result could not find or coerce the structured response."""


class AgentBudgetExhaustedError(AgentError):
    """Agent terminated via middleware budget limit before producing a result."""


class AgentTimeoutError(AgentError):
    """asyncio.wait_for fired on the wall_seconds limit."""


class AgentExecutionError(AgentError):
    """DeepAgents/LangGraph raised before producing any result."""


__all__ = [
    "AgentBudgetExhaustedError",
    "AgentError",
    "AgentExecutionError",
    "AgentProfile",
    "AgentRequest",
    "AgentRunLimits",
    "AgentSandboxForbiddenError",
    "AgentSandboxRequiredError",
    "AgentStructuredOutputError",
    "AgentTimeoutError",
    "DomainResult",
    "SandboxRequirement",
]
```

- [ ] **Step 2: Quick import check**

```bash
uv run python -c "from openbot.infrastructure.agents.profiles import AgentRunLimits, AgentRequest, SandboxRequirement; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add openbot/infrastructure/agents/profiles.py
git commit -m "feat(runtime): add profiles.py with AgentProfile protocol and error types"
```

---

## Task 1.3: _middleware.py + runtime.py + unit tests

**Files:**
- Create: `openbot/infrastructure/agents/_middleware.py`
- Create: `openbot/infrastructure/agents/runtime.py`
- Create: `tests/infrastructure/agents/test_runtime.py`

- [ ] **Step 1: Write the failing runtime tests**

```python
# tests/infrastructure/agents/test_runtime.py
"""Unit tests for BaseDeepAgentRuntime.

All tests monkeypatch create_deep_agent so no network or LLM calls are made.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.tools import BaseTool

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature
from openbot.infrastructure.agents.profiles import (
    AgentBudgetExhaustedError,
    AgentExecutionError,
    AgentRequest,
    AgentRunLimits,
    AgentSandboxForbiddenError,
    AgentSandboxRequiredError,
    AgentTimeoutError,
    SandboxRequirement,
)


# ── Test helpers ──────────────────────────────────────────────────────────────


def _event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="rt-deliv-1",
        kind=EventKind.PR_OPENED,
        repo="org/repo",
        actor="alice",
        pr_number=7,
        installation_id=1,
    )


def _request(**overrides: Any) -> AgentRequest:
    defaults: dict[str, Any] = {
        "event": _event(),
        "input": {"diff": "--- a/x\n+++ b/x\n"},
    }
    defaults.update(overrides)
    return AgentRequest(**defaults)


@dataclass
class _FakeProfile:
    """Minimal profile for runtime testing."""

    feature: Feature = Feature.REVIEW
    agent_name: str = "review"
    response_schema: Any = None
    limits: AgentRunLimits = AgentRunLimits(recursion_limit=5)
    sandbox_requirement: SandboxRequirement = SandboxRequirement.OPTIONAL
    checkpoint_enabled: bool = False
    extra_middleware: Sequence[Any] = ()

    def system_prompt(self, request: AgentRequest) -> str:
        return "You are a test agent."

    def user_message(self, request: AgentRequest) -> str:
        return "Review this diff."

    def build_tools(self, request: AgentRequest) -> Sequence[BaseTool]:
        return []

    def parse_result(self, result: Mapping[str, Any]) -> str:
        messages = result.get("messages", [])
        return getattr(messages[-1], "content", "ok") if messages else "ok"


class _FakeAgent:
    """Fake agent that returns a preset result."""

    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self._result = result
        self.invocations: list[tuple[Any, Any]] = []

    async def ainvoke(self, payload: Any, *, config: Any = None) -> dict[str, Any]:
        self.invocations.append((payload, config))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _StubSandbox:
    pass


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_runtime_constructs_chat_model_not_bare_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime must pass a BaseChatModel object, not a string, to create_deep_agent."""
    import openbot.infrastructure.agents.runtime as mod
    from langchain_core.language_models import BaseChatModel

    captured: dict[str, Any] = {}

    def fake_create_deep_agent(*, model: Any, **_: Any) -> _FakeAgent:
        captured["model"] = model
        return _FakeAgent({"messages": []})

    monkeypatch.setattr(mod, "create_deep_agent", fake_create_deep_agent)

    runtime = mod.BaseDeepAgentRuntime()
    profile = _FakeProfile()
    await runtime.run(profile, _request())

    assert isinstance(captured["model"], BaseChatModel), (
        "Runtime must pass a BaseChatModel to create_deep_agent, never a bare string"
    )


async def test_runtime_normalizes_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model passed to build_agent_chat_model is in provider:name form."""
    import openbot.infrastructure.agents.runtime as mod

    captured: dict[str, Any] = {}
    original_build = mod.build_agent_chat_model

    def spy_build(model: str, limits: Any) -> Any:
        captured["model"] = model
        return original_build(model, limits)

    monkeypatch.setattr(mod, "build_agent_chat_model", spy_build)
    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _FakeAgent({"messages": []}))

    runtime = mod.BaseDeepAgentRuntime()
    await runtime.run(_FakeProfile(), _request())

    assert ":" in captured["model"], f"Expected provider:name form, got {captured['model']!r}"
    assert "/" not in captured["model"], f"Slash not normalized: {captured['model']!r}"


async def test_runtime_passes_recursion_limit_in_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import openbot.infrastructure.agents.runtime as mod

    captured: dict[str, Any] = {}
    fake_agent = _FakeAgent({"messages": []})

    def fake_create(**_: Any) -> _FakeAgent:
        return fake_agent

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    profile = _FakeProfile(limits=AgentRunLimits(recursion_limit=17))
    await mod.BaseDeepAgentRuntime().run(profile, _request())

    config = fake_agent.invocations[0][1]
    assert config["recursion_limit"] == 17


async def test_runtime_attaches_checkpoint_when_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.checkpoint.memory import MemorySaver

    import openbot.infrastructure.agents.runtime as mod

    captured: dict[str, Any] = {}
    fake_agent = _FakeAgent({"messages": []})

    def fake_create(*, checkpointer: Any = None, **_: Any) -> _FakeAgent:
        captured["checkpointer"] = checkpointer
        return fake_agent

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    saver = MemorySaver()
    profile = _FakeProfile(checkpoint_enabled=True)
    request = _request(run_id="run-1", checkpointer=saver)
    await mod.BaseDeepAgentRuntime().run(profile, request)

    assert captured["checkpointer"] is saver
    config = fake_agent.invocations[0][1]
    assert config["configurable"]["thread_id"] == "run-1"


async def test_runtime_no_checkpoint_when_run_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.checkpoint.memory import MemorySaver

    import openbot.infrastructure.agents.runtime as mod

    captured: dict[str, Any] = {}

    def fake_create(*, checkpointer: Any = None, **_: Any) -> _FakeAgent:
        captured["checkpointer"] = checkpointer
        return _FakeAgent({"messages": []})

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    profile = _FakeProfile(checkpoint_enabled=True)
    # checkpointer present but no run_id
    request = _request(checkpointer=MemorySaver())
    await mod.BaseDeepAgentRuntime().run(profile, request)

    assert captured["checkpointer"] is None
    config = _FakeAgent({"messages": []}).invocations  # unused — just check captured
    assert "checkpointer" in captured and captured["checkpointer"] is None


async def test_runtime_no_checkpoint_when_profile_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.checkpoint.memory import MemorySaver

    import openbot.infrastructure.agents.runtime as mod

    captured: dict[str, Any] = {}

    def fake_create(*, checkpointer: Any = None, **_: Any) -> _FakeAgent:
        captured["checkpointer"] = checkpointer
        return _FakeAgent({"messages": []})

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    profile = _FakeProfile(checkpoint_enabled=False)
    request = _request(run_id="run-1", checkpointer=MemorySaver())
    await mod.BaseDeepAgentRuntime().run(profile, request)

    assert captured["checkpointer"] is None


async def test_runtime_raises_when_sandbox_required_but_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbot.infrastructure.agents.runtime as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _FakeAgent({"messages": []}))

    profile = _FakeProfile(sandbox_requirement=SandboxRequirement.REQUIRED)
    request = _request(sandbox=None)

    with pytest.raises(AgentSandboxRequiredError):
        await mod.BaseDeepAgentRuntime().run(profile, request)


async def test_runtime_raises_when_sandbox_forbidden_but_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbot.infrastructure.agents.runtime as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _FakeAgent({"messages": []}))

    profile = _FakeProfile(sandbox_requirement=SandboxRequirement.FORBIDDEN)
    request = _request(sandbox=_StubSandbox())

    with pytest.raises(AgentSandboxForbiddenError):
        await mod.BaseDeepAgentRuntime().run(profile, request)


async def test_runtime_calls_parse_result_and_returns_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbot.infrastructure.agents.runtime as mod

    raw_result = {"messages": [], "structured_response": "parsed_value"}

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _FakeAgent(raw_result))

    parse_calls: list[Any] = []

    class _TrackingProfile(_FakeProfile):
        def parse_result(self, result: Mapping[str, Any]) -> str:
            parse_calls.append(result)
            return "domain_value"

    result = await mod.BaseDeepAgentRuntime().run(_TrackingProfile(), _request())

    assert result == "domain_value"
    assert len(parse_calls) == 1
    assert parse_calls[0] is raw_result


async def test_runtime_wraps_deepagents_exception_in_agent_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbot.infrastructure.agents.runtime as mod

    exc = RuntimeError("deepagents internal error")
    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _FakeAgent(exc))

    with pytest.raises(AgentExecutionError) as exc_info:
        await mod.BaseDeepAgentRuntime().run(_FakeProfile(), _request())

    assert exc_info.value.__cause__ is exc


async def test_runtime_wraps_timeout_in_agent_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbot.infrastructure.agents.runtime as mod

    async def _slow_invoke(payload: Any, *, config: Any = None) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"messages": []}

    class _SlowAgent:
        async def ainvoke(self, payload: Any, *, config: Any = None) -> dict[str, Any]:
            return await _slow_invoke(payload, config=config)

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _SlowAgent())

    profile = _FakeProfile(limits=AgentRunLimits(recursion_limit=5, wall_seconds=0))

    with pytest.raises(AgentTimeoutError):
        await mod.BaseDeepAgentRuntime().run(profile, _request())


async def test_runtime_observability_metadata_uses_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbot.infrastructure.agents.runtime as mod

    captured: dict[str, Any] = {}
    fake_agent = _FakeAgent({"messages": []})

    def fake_create(**_: Any) -> _FakeAgent:
        return fake_agent

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    await mod.BaseDeepAgentRuntime().run(_FakeProfile(), _request())

    config = fake_agent.invocations[0][1]
    meta = config.get("metadata", {})
    model_in_meta = meta.get("model", "")
    assert ":" not in model_in_meta, (
        f"Metadata model should be display_name (no prefix), got {model_in_meta!r}"
    )
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
uv run pytest tests/infrastructure/agents/test_runtime.py -v
```

Expected: `ModuleNotFoundError: No module named 'openbot.infrastructure.agents.runtime'`

- [ ] **Step 3: Implement _middleware.py**

```python
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

logger = logging.getLogger(__name__)


def _canonical_tool_signature(tool_call: dict[str, Any]) -> str:
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
```

- [ ] **Step 4: Implement runtime.py**

```python
# openbot/infrastructure/agents/runtime.py
"""BaseDeepAgentRuntime — shared DeepAgents execution engine.

All production agent invocations go through BaseDeepAgentRuntime.run().
It owns:
  - HTTP client construction (timeout / retries / max_tokens)
  - HarnessProfile registration (disables general_purpose_subagent)
  - Middleware wiring (ToolCallRepetitionGuard → ToolCallLimitMiddleware → ModelCallLimitMiddleware)
  - Checkpoint activation (gates on profile.checkpoint_enabled + run_id + checkpointer)
  - wall_seconds timeout (asyncio.wait_for)
  - Structured output via profile.parse_result()
  - Stable error wrapping (AgentExecutionError, AgentTimeoutError, etc.)

Profiles own only: system_prompt, user_message, build_tools, parse_result, limits.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from deepagents import create_deep_agent
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfileConfig,
    register_harness_profile,
)
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.runnables import RunnableConfig

from openbot.infrastructure.agents._middleware import ToolCallRepetitionGuard
from openbot.infrastructure.agents.model_names import display_name, normalize_for_langchain
from openbot.infrastructure.agents.profiles import (
    AgentBudgetExhaustedError,
    AgentExecutionError,
    AgentProfile,
    AgentRequest,
    AgentRunLimits,
    AgentSandboxForbiddenError,
    AgentSandboxRequiredError,
    AgentTimeoutError,
    DomainResult,
    SandboxRequirement,
)
from openbot.infrastructure.llm.model_router import primary_model_for

if TYPE_CHECKING:
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.language_models import BaseChatModel

_logger = logging.getLogger(__name__)

# Global set of model ids that have had a HarnessProfile registered.
# Registration is idempotent (deepagents merges under the same key), but
# de-duplicating keeps startup quiet.
_REGISTERED_MODELS: set[str] = set()


def build_agent_chat_model(model: str, limits: AgentRunLimits) -> "BaseChatModel":
    """Construct a LangChain chat model with explicit HTTP client configuration.

    Never passes a bare string to create_deep_agent — the httpx default
    has no read timeout, which means a stalled provider socket hangs the
    worker indefinitely.

    Args:
        model:  provider:name form (already normalized via normalize_for_langchain).
        limits: AgentRunLimits carrying timeout/retry/token knobs.
    """
    init_kwargs: dict[str, Any] = {}
    if limits.model_timeout_s is not None:
        init_kwargs["timeout"] = limits.model_timeout_s
    if limits.max_retries is not None:
        init_kwargs["max_retries"] = limits.max_retries
    if limits.max_output_tokens is not None:
        init_kwargs["max_tokens"] = limits.max_output_tokens
    if limits.thinking_budget_tokens > 0:
        init_kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": limits.thinking_budget_tokens,
        }
    return init_chat_model(model, **init_kwargs)


def _register_harness_profile(model: str) -> None:
    """Register the OpenBot baseline HarnessProfile for model (idempotent).

    Disabling general_purpose_subagent prevents the auto-attached 'task'
    delegation tool from branching single-objective production runs.
    """
    if model in _REGISTERED_MODELS:
        return
    register_harness_profile(
        model,
        HarnessProfileConfig(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    _REGISTERED_MODELS.add(model)


def _build_standard_middleware(limits: AgentRunLimits) -> list["AgentMiddleware"]:
    """Standard safety middleware: repetition guard → tool cap → model cap."""
    stack: list[AgentMiddleware] = [ToolCallRepetitionGuard()]
    if limits.tool_call_limit is not None:
        stack.append(
            ToolCallLimitMiddleware(
                thread_limit=limits.tool_call_limit,
                exit_behavior="continue",  # let model emit final answer
            )
        )
    if limits.model_call_limit is not None:
        stack.append(
            ModelCallLimitMiddleware(
                thread_limit=limits.model_call_limit,
                exit_behavior="end",
            )
        )
    return stack


def _validate_sandbox(
    profile: AgentProfile[Any], request: AgentRequest
) -> None:
    """Raise if the sandbox presence doesn't match the profile's requirement."""
    req = profile.sandbox_requirement
    has_sandbox = request.sandbox is not None
    if req == SandboxRequirement.REQUIRED and not has_sandbox:
        raise AgentSandboxRequiredError(
            f"Profile '{profile.agent_name}' requires a sandbox but request.sandbox is None."
        )
    if req == SandboxRequirement.FORBIDDEN and has_sandbox:
        raise AgentSandboxForbiddenError(
            f"Profile '{profile.agent_name}' forbids a sandbox but request includes one."
        )


class BaseDeepAgentRuntime:
    """Shared DeepAgents execution engine.

    One instance is typically created per responder class and reused across
    requests. The runtime itself is stateless — all per-invocation state
    lives in AgentRequest and the compiled agent graph.
    """

    async def run(
        self,
        profile: AgentProfile[DomainResult],
        request: AgentRequest,
    ) -> DomainResult:
        """Run the agent described by profile with the given request.

        Raises:
            AgentSandboxRequiredError:   profile requires sandbox, none present.
            AgentSandboxForbiddenError:  profile forbids sandbox, one present.
            AgentBudgetExhaustedError:   middleware budget exhausted before result.
            AgentTimeoutError:           wall_seconds limit exceeded.
            AgentExecutionError:         any other DeepAgents/LangGraph failure.
        """
        _validate_sandbox(profile, request)

        raw_model = primary_model_for(profile.feature)
        model = normalize_for_langchain(raw_model)
        _register_harness_profile(model)

        chat_model = build_agent_chat_model(model, profile.limits)

        middleware = _build_standard_middleware(profile.limits)
        middleware.extend(profile.extra_middleware)

        tools = list(profile.build_tools(request))

        # Checkpoint only when all three conditions are met.
        effective_checkpointer = None
        if profile.checkpoint_enabled and request.run_id and request.checkpointer is not None:
            effective_checkpointer = request.checkpointer

        agent = create_deep_agent(
            model=chat_model,
            tools=tools,
            system_prompt=profile.system_prompt(request),
            response_format=profile.response_schema,
            middleware=middleware,
            checkpointer=effective_checkpointer,
        )

        config = RunnableConfig(
            recursion_limit=profile.limits.recursion_limit,
            metadata={
                "feature": profile.feature.value,
                "agent_profile": profile.agent_name,
                "run_id": request.run_id,
                "task_id": request.event.delivery_id,
                "repo": request.event.repo,
                "actor": request.event.actor,
                "model": display_name(model),
                "checkpoint_enabled": effective_checkpointer is not None,
                "sandbox_present": request.sandbox is not None,
                **dict(request.metadata),
            },
        )
        if effective_checkpointer is not None:
            config["configurable"] = {"thread_id": request.run_id}

        invoke_coro = agent.ainvoke(
            {"messages": [{"role": "user", "content": profile.user_message(request)}]},
            config=config,
        )

        try:
            if profile.limits.wall_seconds is not None:
                raw = await asyncio.wait_for(
                    invoke_coro, timeout=profile.limits.wall_seconds
                )
            else:
                raw = await invoke_coro
        except asyncio.TimeoutError as exc:
            raise AgentTimeoutError(
                f"Agent '{profile.agent_name}' exceeded wall_seconds={profile.limits.wall_seconds}"
            ) from exc
        except Exception as exc:
            # Check for budget exhaustion (middleware raises a termination error).
            exc_type_name = type(exc).__name__
            if "Termination" in exc_type_name or "Budget" in exc_type_name:
                raise AgentBudgetExhaustedError(
                    f"Agent '{profile.agent_name}' budget exhausted"
                ) from exc
            raise AgentExecutionError(
                f"Agent '{profile.agent_name}' failed: {type(exc).__name__}"
            ) from exc

        return profile.parse_result(raw)


__all__ = ["BaseDeepAgentRuntime", "build_agent_chat_model"]
```

- [ ] **Step 5: Run tests — expect PASS**

```bash
uv run pytest tests/infrastructure/agents/test_runtime.py -v
```

Expected: all 10 tests pass.

- [ ] **Step 6: Run full agent test suite to confirm no regressions**

```bash
uv run pytest tests/infrastructure/agents/ -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add openbot/infrastructure/agents/_middleware.py \
        openbot/infrastructure/agents/runtime.py \
        tests/infrastructure/agents/test_runtime.py
git commit -m "feat(runtime): add BaseDeepAgentRuntime with middleware stack and HTTP client config"
```

---

## Task 2.1: ReviewProfile + wrapper migration

**Files:**
- Modify: `openbot/infrastructure/agents/deepagents_review.py`
- Modify: `tests/infrastructure/agents/test_deepagents_review.py`

- [ ] **Step 1: Rewrite deepagents_review.py**

Replace the entire file contents with:

```python
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

from openbot.domain.events import UnifiedEvent
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

_MAX_DIFF_CHARS = 64_000


def _truncate_diff(diff: str) -> str:
    if len(diff) <= _MAX_DIFF_CHARS:
        return diff
    head = diff[:_MAX_DIFF_CHARS]
    dropped = len(diff) - _MAX_DIFF_CHARS
    return f"{head}\n\n(diff truncated — dropped {dropped} chars; review remaining changes manually)"


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
    tool_call_limit=8,    # slightly above ToolBudget=5 during transition
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
    response_schema: type[ReviewFindingsSchema] = field(
        default=ReviewFindingsSchema, init=False
    )
    limits: AgentRunLimits = field(default=_REVIEW_LIMITS, init=False)
    sandbox_requirement: SandboxRequirement = field(
        default=SandboxRequirement.OPTIONAL, init=False
    )
    checkpoint_enabled: bool = field(default=True, init=False)
    extra_middleware: Sequence[Any] = field(default_factory=list, init=False)

    # Per-invocation state set during build_tools / user_message
    _adapter: ChannelAdapterPort | None = field(default=None, init=False, repr=False)
    _event: UnifiedEvent | None = field(default=None, init=False, repr=False)

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
    """Compatibility wrapper — delegates to BaseDeepAgentRuntime.

    Use cases retain their current import. Internally, this class
    constructs an AgentRequest and runs it through the shared runtime.
    """

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
        # Diff fetch happens here so ReviewProfile remains a pure function.
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
```

- [ ] **Step 2: Update existing review tests to assert delegation**

Open `tests/infrastructure/agents/test_deepagents_review.py`. The existing tests monkeypatch `create_deep_agent` at the module level. After the migration, `create_deep_agent` is in `runtime.py`, so the monkeypatch target changes.

Add this import at the top of the test file and update any monkeypatch targets:

```python
# In each test that uses monkeypatch, change:
#   monkeypatch.setattr(mod, "create_deep_agent", ...)
# to:
import openbot.infrastructure.agents.runtime as runtime_mod
monkeypatch.setattr(runtime_mod, "create_deep_agent", ...)
```

Also add a new delegation assertion test:

```python
async def test_review_responder_delegates_to_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compatibility wrapper must delegate to BaseDeepAgentRuntime."""
    import openbot.infrastructure.agents.runtime as runtime_mod
    from openbot.infrastructure.agents.deepagents_review import (
        DeepAgentsReviewResponder,
        ReviewProfile,
    )

    run_calls: list[tuple[Any, Any]] = []

    async def fake_run(profile: Any, request: Any) -> ReviewFindings:
        run_calls.append((profile, request))
        return ReviewFindings(summary="delegated", findings=())

    monkeypatch.setattr(runtime_mod, "create_deep_agent", lambda **_: _Agent())  # type: ignore

    from openbot.infrastructure.agents.runtime import BaseDeepAgentRuntime

    monkeypatch.setattr(BaseDeepAgentRuntime, "run", fake_run)

    adapter = _StubAdapter("--- a/x\n+++ b/x")
    result = await DeepAgentsReviewResponder().review_for_event(_event(), adapter=adapter)  # type: ignore[arg-type]

    assert len(run_calls) == 1
    assert isinstance(run_calls[0][0], ReviewProfile)
    assert result.summary == "delegated"
```

- [ ] **Step 3: Run review tests**

```bash
uv run pytest tests/infrastructure/agents/test_deepagents_review.py -v
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add openbot/infrastructure/agents/deepagents_review.py \
        tests/infrastructure/agents/test_deepagents_review.py
git commit -m "feat(runtime): migrate review responder to ReviewProfile + BaseDeepAgentRuntime"
```

---

## Task 2.2: Fix review use case to pass run_id + checkpointer

**Files:**
- Modify: `openbot/application/use_cases/review.py`
- Modify: `tests/application/use_cases/test_review.py`

The review use case currently calls `_generate_review_findings(event=event, adapter=ctx.adapter)` without checkpointing. Fix adds `run_id` and `checkpointer`.

- [ ] **Step 1: Write a failing test for checkpoint pass-through**

Add this test to `tests/application/use_cases/test_review.py`:

```python
async def test_review_use_case_passes_run_id_and_checkpointer_to_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """maybe_run_review must forward ctx.dispatch.run_id and ctx.agent_checkpointer."""
    from langgraph.checkpoint.memory import MemorySaver

    import openbot.application.use_cases.review as review_mod

    captured: dict[str, Any] = {}

    async def fake_generate(
        *,
        event: Any,
        adapter: Any,
        run_id: str | None = None,
        checkpointer: Any = None,
    ) -> ReviewFindings:
        captured["run_id"] = run_id
        captured["checkpointer"] = checkpointer
        return ReviewFindings(summary="ok", findings=())

    monkeypatch.setattr(review_mod, "_generate_review_findings", fake_generate)

    saver = MemorySaver()
    event = _event()
    adapter = _adapter()
    ctx = _ctx(adapter, event)
    # Manually attach run_id and checkpointer to the context
    object.__setattr__(ctx.dispatch, "run_id", "review-run-42")
    ctx = ctx.__class__(
        event=ctx.event,
        dispatch=ctx.dispatch,
        config=ctx.config,
        adapter=ctx.adapter,
        session_factory=ctx.session_factory,
        redis=ctx.redis,
        agent_checkpointer=saver,
    )

    await review_mod.maybe_run_review(ctx)

    assert captured["run_id"] == "review-run-42"
    assert captured["checkpointer"] is saver
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
uv run pytest tests/application/use_cases/test_review.py::test_review_use_case_passes_run_id_and_checkpointer_to_responder -v
```

Expected: FAIL — `_generate_review_findings` doesn't accept `run_id` / `checkpointer`.

- [ ] **Step 3: Update review.py**

Find and update `_generate_review_findings` and its call site in `review.py`:

```python
# Replace this:
async def _generate_review_findings(
    *, event: UnifiedEvent, adapter: ChannelAdapterPort
) -> ReviewFindings:
    """Module-level seam — E2E tests monkeypatch this to avoid LLM calls."""
    return await _RESPONDER.review_for_event(event, adapter=adapter)

# With this:
async def _generate_review_findings(
    *,
    event: UnifiedEvent,
    adapter: ChannelAdapterPort,
    run_id: str | None = None,
    checkpointer: "BaseCheckpointSaver | None" = None,
) -> ReviewFindings:
    """Module-level seam — E2E tests monkeypatch this to avoid LLM calls."""
    return await _RESPONDER.review_for_event(
        event, adapter=adapter, run_id=run_id, checkpointer=checkpointer
    )
```

Also update the call site in `maybe_run_review`:

```python
# Find the line:
findings = await _generate_review_findings(event=event, adapter=ctx.adapter)

# Replace with:
findings = await _generate_review_findings(
    event=event,
    adapter=ctx.adapter,
    run_id=ctx.dispatch.run_id,
    checkpointer=ctx.agent_checkpointer,
)
```

Add the `TYPE_CHECKING` import for `BaseCheckpointSaver` at the top of `review.py`:

```python
if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
```

- [ ] **Step 4: Run the test — expect PASS**

```bash
uv run pytest tests/application/use_cases/test_review.py -v
```

Expected: all pass.

- [ ] **Step 5: Full regression check**

```bash
uv run pytest tests/infrastructure/agents/ tests/application/use_cases/ -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add openbot/application/use_cases/review.py tests/application/use_cases/test_review.py
git commit -m "fix(review): pass run_id + agent_checkpointer from use case to responder"
```

---

## Task 3.1: FixProfile + wrapper migration

**Files:**
- Modify: `openbot/infrastructure/agents/deepagents_fix.py`
- Modify: `tests/infrastructure/agents/test_deepagents_fix.py`

- [ ] **Step 1: Rewrite deepagents_fix.py**

Replace entire file contents:

```python
# openbot/infrastructure/agents/deepagents_fix.py
"""Fix profile and compatibility wrapper — migrated to BaseDeepAgentRuntime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool

from openbot.application.ports.sandbox import SandboxPort
from openbot.domain.events import UnifiedEvent
from openbot.domain.fix import FixOutcome
from openbot.domain.workflows import Feature
from openbot.infrastructure.agents._fix_schema import (
    FixOutcomeSchema,
    parse_structured_response,
)
from openbot.infrastructure.agents._fix_tools import make_fix_tools
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

_SYSTEM_PROMPT = """You are OpenBot, a senior engineer. You will fix the bug \
described in the GitHub issue below by editing files in the sandbox and \
running tests until they pass. Return a JSON object matching the schema — \
never plain text.

Workflow:
- Read the issue carefully. Form a hypothesis about which file(s) are wrong.
- Use `list_files` and `search_files` to navigate; use `read_file` to inspect.
- Use `write_file` to apply the smallest possible change that fixes the bug.
- Use `run_command` to run the project's test suite.
- If tests fail, iterate: re-read code, refine the fix, re-run tests.
- When tests pass, use `git_diff` to capture the final diff and return your structured answer.

Tools available (total tool calls are budget-capped — stop iterating before you exhaust):
- `read_file(path)` — read a UTF-8 file from the sandbox working tree.
- `write_file(path, content)` — overwrite or create a file.
- `list_files(path=".")` — list a directory's entries (non-recursive).
- `run_command(command)` — run a shell command in the sandbox.
- `git_diff()` — return `git diff` against the base commit. Call once near the end.
- `search_files(pattern, path_glob="**/*")` — recursive grep in the working tree.

Rules:
- Make the smallest change that fixes the bug. Do not refactor unrelated code.
- Tests must pass on the final attempt — set `tests_passed=false` only if you \
genuinely could not fix it within your budget.
- Return ONE structured object. Do not emit chains of thought or markdown prose.
- Keep `summary` to one line. `files_changed` lists repo-relative paths you wrote.
- `test_output` should be the tail of the final test run (≤ 2000 chars).
"""

_FIX_LIMITS = AgentRunLimits(
    recursion_limit=60,
    tool_call_limit=25,      # slightly above ToolBudget=20 during transition
    model_call_limit=20,
    wall_seconds=1800,       # 30-minute hard ceiling
    model_timeout_s=300,
    max_retries=2,
    max_output_tokens=16_384,
)


@dataclass
class FixProfile:
    """Profile for the issue-fix agent."""

    feature: Feature = field(default=Feature.FIX, init=False)
    agent_name: str = field(default="fix", init=False)
    response_schema: type[FixOutcomeSchema] = field(
        default=FixOutcomeSchema, init=False
    )
    limits: AgentRunLimits = field(default=_FIX_LIMITS, init=False)
    sandbox_requirement: SandboxRequirement = field(
        default=SandboxRequirement.REQUIRED, init=False
    )
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
        assert isinstance(request.sandbox, SandboxPort), (
            "FixProfile.build_tools requires a SandboxPort in request.sandbox"
        )
        return make_fix_tools(sandbox=request.sandbox, event=request.event)

    def parse_result(self, result: Mapping[str, Any]) -> FixOutcome:
        structured = result.get("structured_response")
        if structured is None:
            raise AgentStructuredOutputError("deepagents_fix_result_missing_structured_response")
        return parse_structured_response(structured)


class DeepAgentsFixResponder:
    """Compatibility wrapper — delegates to BaseDeepAgentRuntime."""

    def __init__(self, runtime: BaseDeepAgentRuntime | None = None) -> None:
        self._runtime = runtime or BaseDeepAgentRuntime()

    async def fix_for_event(
        self,
        event: UnifiedEvent,
        *,
        adapter: "ChannelAdapterPort",
        sandbox: SandboxPort,
        issue: dict[str, Any],
        run_id: str | None = None,
        checkpointer: "BaseCheckpointSaver | None" = None,
    ) -> FixOutcome:
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
            ),
        )


__all__ = ["DeepAgentsFixResponder", "FixProfile"]
```

- [ ] **Step 2: Update fix tests to patch runtime.create_deep_agent**

In `tests/infrastructure/agents/test_deepagents_fix.py`, change all `monkeypatch.setattr(mod, "create_deep_agent", ...)` to:

```python
import openbot.infrastructure.agents.runtime as runtime_mod
monkeypatch.setattr(runtime_mod, "create_deep_agent", fake_create_deep_agent)
```

- [ ] **Step 3: Run fix tests**

```bash
uv run pytest tests/infrastructure/agents/test_deepagents_fix.py -v
```

Expected: all pass.

- [ ] **Step 4: Verify wall_seconds limit is wired**

Add this test to `tests/infrastructure/agents/test_deepagents_fix.py`:

```python
async def test_fix_profile_has_wall_seconds_limit() -> None:
    """FixProfile must declare a wall_seconds ceiling to cap runaway fix loops."""
    from openbot.infrastructure.agents.deepagents_fix import FixProfile

    profile = FixProfile()
    assert profile.limits.wall_seconds is not None
    assert profile.limits.wall_seconds >= 60, "wall_seconds should be at least 60 seconds"
```

```bash
uv run pytest tests/infrastructure/agents/test_deepagents_fix.py::test_fix_profile_has_wall_seconds_limit -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add openbot/infrastructure/agents/deepagents_fix.py \
        tests/infrastructure/agents/test_deepagents_fix.py
git commit -m "feat(runtime): migrate fix responder to FixProfile + BaseDeepAgentRuntime"
```

---

## Task 4.1: ChatProfile + wrapper migration

**Files:**
- Modify: `openbot/infrastructure/agents/deepagents_chat.py`
- Modify: `tests/infrastructure/agents/test_deepagents_chat.py`

The current chat responder has a bug: it passes `config = {}` or `config or None` to `ainvoke`, meaning LangGraph gets no `recursion_limit`. This task fixes it.

- [ ] **Step 1: Write a failing test for the recursion_limit bug**

Add to `tests/infrastructure/agents/test_deepagents_chat.py`:

```python
async def test_chat_profile_sets_recursion_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chat must receive a recursion_limit — the current None/empty config is a bug."""
    import openbot.infrastructure.agents.runtime as runtime_mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, *, config: Any = None) -> dict[str, Any]:
            captured["config"] = config
            msg = type("M", (), {"content": "hello"})()
            return {"messages": [msg]}

    monkeypatch.setattr(runtime_mod, "create_deep_agent", lambda **_: FakeAgent())

    from openbot.infrastructure.agents.deepagents_chat import DeepAgentsChatResponder

    event = UnifiedEvent(
        channel="github",
        delivery_id="c-1",
        kind=EventKind.ISSUE_COMMENT,
        repo="o/r",
        actor="bob",
        installation_id=1,
    )
    await DeepAgentsChatResponder().reply_for_event(event, user_request="Hello!")

    assert captured.get("config") is not None
    assert "recursion_limit" in captured["config"]
    assert captured["config"]["recursion_limit"] > 0
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
uv run pytest tests/infrastructure/agents/test_deepagents_chat.py::test_chat_profile_sets_recursion_limit -v
```

Expected: FAIL — current code passes `config or None`.

- [ ] **Step 3: Rewrite deepagents_chat.py**

```python
# openbot/infrastructure/agents/deepagents_chat.py
"""Chat profile and compatibility wrapper — migrated to BaseDeepAgentRuntime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool

from openbot.domain.events import UnifiedEvent
from openbot.domain.workflows import Feature
from openbot.infrastructure.agents.profiles import (
    AgentRequest,
    AgentRunLimits,
    SandboxRequirement,
)
from openbot.infrastructure.agents.runtime import BaseDeepAgentRuntime

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

_SYSTEM_PROMPT = """You are OpenBot, a GitHub maintainer bot assistant.

You are answering a GitHub comment mention inside an automation workflow.

Rules:
- Answer the user's request directly and concisely.
- Use only the context provided in the prompt.
- Do not claim you inspected repository files, ran commands, or fetched remote data unless that context is explicitly provided.
- If the user asks for action you cannot complete from the provided context, say so clearly and suggest the next concrete step.
"""

_CHAT_LIMITS = AgentRunLimits(
    recursion_limit=10,
    model_call_limit=3,
    model_timeout_s=60,
    max_output_tokens=4_096,
)


def _target_label(event: UnifiedEvent) -> str:
    if event.issue_number is not None:
        return f"issue #{event.issue_number}"
    if event.pr_number is not None:
        return f"pull request #{event.pr_number}"
    return "GitHub thread"


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


@dataclass
class ChatProfile:
    """Profile for the chat/mention reply agent."""

    feature: Feature = field(default=Feature.CHAT, init=False)
    agent_name: str = field(default="chat", init=False)
    response_schema: None = field(default=None, init=False)
    limits: AgentRunLimits = field(default=_CHAT_LIMITS, init=False)
    sandbox_requirement: SandboxRequirement = field(
        default=SandboxRequirement.FORBIDDEN, init=False
    )
    checkpoint_enabled: bool = field(default=False, init=False)
    extra_middleware: Sequence[Any] = field(default_factory=list, init=False)

    def system_prompt(self, request: AgentRequest) -> str:
        return _SYSTEM_PROMPT

    def user_message(self, request: AgentRequest) -> str:
        event = request.event
        user_request = str(request.input.get("user_request", ""))
        return (
            "GitHub context:\n"
            f"- repository: {event.repo}\n"
            f"- target: {_target_label(event)}\n"
            f"- actor: {event.actor}\n"
            f"- event kind: {event.kind.value}\n\n"
            "User request:\n"
            f"{user_request}"
        )

    def build_tools(self, request: AgentRequest) -> Sequence[BaseTool]:
        return []

    def parse_result(self, result: Mapping[str, Any]) -> str:
        messages = result.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("deepagents_result_missing_messages")
        content = getattr(messages[-1], "content", None)
        reply = _extract_message_text(content)
        if not reply:
            raise ValueError("deepagents_result_missing_text")
        return reply


class DeepAgentsChatResponder:
    """Compatibility wrapper — delegates to BaseDeepAgentRuntime.

    The previous implementation passed `config or None` to ainvoke, meaning
    LangGraph received no recursion_limit. The runtime always sets it from
    profile.limits.recursion_limit.
    """

    def __init__(self, runtime: BaseDeepAgentRuntime | None = None) -> None:
        self._runtime = runtime or BaseDeepAgentRuntime()

    async def reply_for_event(
        self,
        event: UnifiedEvent,
        *,
        user_request: str,
        run_id: str | None = None,
        checkpointer: "BaseCheckpointSaver | None" = None,
    ) -> str:
        return await self._runtime.run(
            ChatProfile(),
            AgentRequest(
                event=event,
                run_id=run_id,
                checkpointer=checkpointer,
                input={"user_request": user_request},
            ),
        )


__all__ = ["ChatProfile", "DeepAgentsChatResponder"]
```

- [ ] **Step 4: Run chat tests**

```bash
uv run pytest tests/infrastructure/agents/test_deepagents_chat.py -v
```

Expected: all pass including the new recursion_limit test.

- [ ] **Step 5: Full regression gate**

```bash
uv run pytest tests/infrastructure/agents/ tests/application/use_cases/ tests/application/test_dispatcher.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add openbot/infrastructure/agents/deepagents_chat.py \
        tests/infrastructure/agents/test_deepagents_chat.py
git commit -m "feat(runtime): migrate chat responder to ChatProfile + BaseDeepAgentRuntime (fix: recursion_limit bug)"
```

---

## Task 5.1: Retire ToolBudget from _review_tools.py

**Files:**
- Modify: `openbot/infrastructure/agents/_review_tools.py`
- Modify: `tests/infrastructure/agents/test_review_tools.py`

Now that `ToolCallLimitMiddleware` is active with `tool_call_limit=8`, the hand-rolled `ToolBudget` is redundant. Set the final limit in the profile to 5 (the retired budget value) and remove `ToolBudget`.

- [ ] **Step 1: Update ReviewProfile tool_call_limit to 5**

In `openbot/infrastructure/agents/deepagents_review.py`, change `_REVIEW_LIMITS`:

```python
_REVIEW_LIMITS = AgentRunLimits(
    recursion_limit=25,
    tool_call_limit=5,    # matches retired ToolBudget value
    model_call_limit=10,
    model_timeout_s=120,
    max_retries=2,
    max_output_tokens=16_384,
)
```

- [ ] **Step 2: Remove ToolBudget from _review_tools.py**

Replace `_review_tools.py` with the stripped version (keep only tools, remove ToolBudget):

```python
# openbot/infrastructure/agents/_review_tools.py
"""LangChain tool wrappers for the review agent.

Tools close over (adapter, event). The per-tool budget is now enforced
by ToolCallLimitMiddleware in the runtime stack — ToolBudget is retired.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.tools import StructuredTool

if TYPE_CHECKING:
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.domain.events import UnifiedEvent


def make_review_tools(
    *,
    adapter: "ChannelAdapterPort",
    event: "UnifiedEvent",
) -> list[StructuredTool]:
    """Build the per-run review tool list."""

    async def read_file(path: str) -> str:
        return await adapter.read_file(event, path)

    async def grep_repo(
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        return await adapter.grep_repo(
            event,
            pattern=pattern,
            path_glob=path_glob,
            max_matches=max_matches,
        )

    return [
        StructuredTool.from_function(
            coroutine=read_file,
            name="read_file",
            description=(
                "Read the UTF-8 text of a file in the repository. "
                "Returns an empty string if the file is missing or not decodable."
            ),
        ),
        StructuredTool.from_function(
            coroutine=grep_repo,
            name="grep_repo",
            description=(
                "Search the repository for a pattern via GitHub Code Search. "
                "`path_glob` is GitHub's `path:` qualifier. "
                "Returns up to `max_matches` lines formatted `path: fragment`."
            ),
        ),
    ]


__all__ = ["make_review_tools"]
```

- [ ] **Step 3: Update review_tools tests — remove ToolBudget tests**

In `tests/infrastructure/agents/test_review_tools.py`, remove any test that imports or exercises `ToolBudget` or `ToolBudgetExceededError`. Add a basic make_review_tools smoke test if none exists:

```python
async def test_make_review_tools_returns_two_tools() -> None:
    from openbot.infrastructure.agents._review_tools import make_review_tools
    from openbot.domain.events import EventKind, UnifiedEvent

    class _StubAdapter:
        async def read_file(self, event: Any, path: str) -> str:
            return ""
        async def grep_repo(self, event: Any, **kwargs: Any) -> list[str]:
            return []

    event = UnifiedEvent(
        channel="github", delivery_id="d", kind=EventKind.PR_OPENED,
        repo="o/r", actor="alice", installation_id=1,
    )
    tools = make_review_tools(adapter=_StubAdapter(), event=event)  # type: ignore[arg-type]
    names = {t.name for t in tools}
    assert names == {"read_file", "grep_repo"}
```

- [ ] **Step 4: Run review tools tests**

```bash
uv run pytest tests/infrastructure/agents/test_review_tools.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add openbot/infrastructure/agents/_review_tools.py \
        openbot/infrastructure/agents/deepagents_review.py \
        tests/infrastructure/agents/test_review_tools.py
git commit -m "refactor(runtime): retire ToolBudget from _review_tools — ToolCallLimitMiddleware takes over"
```

---

## Task 5.2: Retire ToolBudget from _fix_tools.py + final cleanup

**Files:**
- Modify: `openbot/infrastructure/agents/_fix_tools.py`
- Modify: `tests/infrastructure/agents/test_fix_tools.py`
- Modify: `openbot/infrastructure/agents/__init__.py`

- [ ] **Step 1: Update FixProfile tool_call_limit to 20**

In `openbot/infrastructure/agents/deepagents_fix.py`, change `_FIX_LIMITS`:

```python
_FIX_LIMITS = AgentRunLimits(
    recursion_limit=60,
    tool_call_limit=20,      # matches retired ToolBudget value
    model_call_limit=20,
    wall_seconds=1800,
    model_timeout_s=300,
    max_retries=2,
    max_output_tokens=16_384,
)
```

- [ ] **Step 2: Remove ToolBudget from _fix_tools.py**

Replace `_fix_tools.py`:

```python
# openbot/infrastructure/agents/_fix_tools.py
"""LangChain tool wrappers for the fix agent.

Tools close over (sandbox, event). Budget is enforced by
ToolCallLimitMiddleware in the runtime stack — ToolBudget is retired.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.tools import StructuredTool

if TYPE_CHECKING:
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.domain.events import UnifiedEvent


def _exec_result_to_dict(result: Any) -> dict[str, Any]:
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
    }


def make_fix_tools(
    *,
    sandbox: "SandboxPort",
    event: "UnifiedEvent",  # reserved for per-event logging
) -> list[StructuredTool]:
    """Build the per-run fix tool list."""

    async def read_file(path: str) -> str:
        return await sandbox.read_file(path)

    async def write_file(path: str, content: str) -> str:
        await sandbox.write_file(path, content)
        return f"wrote {len(content)} bytes to {path}"

    async def list_files(path: str = ".", max: int = 200) -> list[str]:
        return await sandbox.list_files(path=path, max=max)

    async def run_command(command: list[str], timeout_seconds: int = 60) -> dict[str, Any]:
        result = await sandbox.run(command=command, timeout_seconds=timeout_seconds)
        return _exec_result_to_dict(result)

    async def git_diff() -> str:
        return await sandbox.git_diff()

    async def search_files(pattern: str, path_glob: str | None = None) -> list[str]:
        cmd: list[str] = ["grep", "-rn", pattern]
        if path_glob:
            cmd.extend(["--include", path_glob])
        cmd.append(".")
        result = await sandbox.run(command=cmd, timeout_seconds=30)
        if result.exit_code not in (0, 1):
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    return [
        StructuredTool.from_function(
            coroutine=read_file, name="read_file",
            description="Read a UTF-8 file from the sandbox workspace.",
        ),
        StructuredTool.from_function(
            coroutine=write_file, name="write_file",
            description="Replace a file's contents in the sandbox workspace.",
        ),
        StructuredTool.from_function(
            coroutine=list_files, name="list_files",
            description="List files under `path` (default '.') up to `max` results.",
        ),
        StructuredTool.from_function(
            coroutine=run_command, name="run_command",
            description=(
                "Run an argv-list command in the workspace. Returns "
                "{stdout, stderr, exit_code, timed_out}."
            ),
        ),
        StructuredTool.from_function(
            coroutine=git_diff, name="git_diff",
            description="Return the working-tree diff after your edits.",
        ),
        StructuredTool.from_function(
            coroutine=search_files, name="search_files",
            description=(
                "grep -rn for `pattern` across the workspace, optionally "
                "filtered by `path_glob`. Returns lines formatted `path:line:fragment`."
            ),
        ),
    ]


__all__ = ["make_fix_tools"]
```

- [ ] **Step 3: Update fix_tools tests**

In `tests/infrastructure/agents/test_fix_tools.py`, remove any test that exercises `ToolBudget` or `ToolBudgetExceededError`. Keep integration-style tests that confirm tool names and basic plumbing. Add:

```python
async def test_make_fix_tools_returns_six_tools() -> None:
    from openbot.infrastructure.agents._fix_tools import make_fix_tools
    from openbot.domain.events import EventKind, UnifiedEvent

    class _StubSandbox:
        async def read_file(self, path: str) -> str:
            return ""
        async def write_file(self, path: str, content: str) -> None:
            pass
        async def list_files(self, path: str = ".", max: int = 200) -> list[str]:
            return []
        async def run(self, command: list[str], timeout_seconds: int = 60) -> Any:
            return type("R", (), {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False})()
        async def git_diff(self) -> str:
            return ""

    event = UnifiedEvent(
        channel="github", delivery_id="d", kind=EventKind.ISSUE_ASSIGNED,
        repo="o/r", actor="alice", issue_number=1, installation_id=1,
    )
    tools = make_fix_tools(sandbox=_StubSandbox(), event=event)  # type: ignore[arg-type]
    names = {t.name for t in tools}
    assert names == {"read_file", "write_file", "list_files", "run_command", "git_diff", "search_files"}
```

- [ ] **Step 4: Update __init__.py to re-export runtime types**

```python
# openbot/infrastructure/agents/__init__.py
"""DeepAgents-backed runtime adapters for OpenBot workflows."""

from openbot.infrastructure.agents.deepagents_chat import DeepAgentsChatResponder
from openbot.infrastructure.agents.deepagents_fix import DeepAgentsFixResponder
from openbot.infrastructure.agents.deepagents_review import DeepAgentsReviewResponder
from openbot.infrastructure.agents.profiles import AgentProfile, AgentRequest, AgentRunLimits
from openbot.infrastructure.agents.runtime import BaseDeepAgentRuntime

__all__ = [
    "AgentProfile",
    "AgentRequest",
    "AgentRunLimits",
    "BaseDeepAgentRuntime",
    "DeepAgentsChatResponder",
    "DeepAgentsFixResponder",
    "DeepAgentsReviewResponder",
]
```

- [ ] **Step 5: Full final gate**

```bash
uv run make check
```

If `make check` is not available:

```bash
uv run pytest tests/infrastructure/agents/ tests/application/use_cases/ -q && \
uv run ruff check openbot/infrastructure/agents/ && \
uv run ruff format --check openbot/infrastructure/agents/
```

Expected: all pass, no lint warnings.

- [ ] **Step 6: Verify no production module imports from evals**

```bash
grep -rn "from evals" openbot/ && echo "FOUND evals imports — fix before merging" || echo "clean"
```

Expected: `clean`

- [ ] **Step 7: Commit**

```bash
git add openbot/infrastructure/agents/_fix_tools.py \
        openbot/infrastructure/agents/deepagents_fix.py \
        openbot/infrastructure/agents/__init__.py \
        tests/infrastructure/agents/test_fix_tools.py
git commit -m "refactor(runtime): retire ToolBudget from _fix_tools, update __init__ exports"
```

---

## Self-Review

### Spec coverage check

| Spec requirement | Task |
|---|---|
| `model_names.py` with `normalize_for_langchain` + `display_name` | Task 1.1 |
| `profiles.py` with `AgentProfile`, `AgentRequest`, `AgentRunLimits`, errors | Task 1.2 |
| `ToolCallRepetitionGuard` production-safe copy | Task 1.3 |
| `build_agent_chat_model` (BaseChatModel, not bare string) | Task 1.3 |
| `BaseDeepAgentRuntime` with middleware, checkpoint, wall_seconds | Task 1.3 |
| HarnessProfile registration per model | Task 1.3 |
| Observability metadata including `display_name` model | Task 1.3 |
| `ReviewProfile` + compatibility wrapper | Task 2.1 |
| Review use case passes `run_id` + `agent_checkpointer` | Task 2.2 |
| `FixProfile` + compatibility wrapper | Task 3.1 |
| `wall_seconds=1800` on fix profile | Task 3.1 |
| `ChatProfile` + fix recursion_limit=None bug | Task 4.1 |
| Retire `ToolBudget` from review tools | Task 5.1 |
| Retire `ToolBudget` from fix tools | Task 5.2 |
| `__init__.py` re-exports `BaseDeepAgentRuntime` | Task 5.2 |
| No `evals.*` import in production | Task 5.2 verification step |

All spec sections have a corresponding task. ✓

### Type consistency check

- `AgentRunLimits` defined in Task 1.2, used in Tasks 2.1, 3.1, 4.1. Field names match: `recursion_limit`, `tool_call_limit`, `model_call_limit`, `wall_seconds`, `model_timeout_s`, `max_retries`, `max_output_tokens`. ✓
- `AgentRequest.input` uses dict keys `{"diff": str}`, `{"issue_title", "issue_body", "base_sha": str}`, `{"user_request": str}` consistently across profile `user_message` methods. ✓
- `SandboxRequirement.OPTIONAL/REQUIRED/FORBIDDEN` constants match Profile declarations. ✓
- `profile.agent_name` (not `name`) consistent across all profiles and runtime metadata field `"agent_profile"`. ✓
- `AgentStructuredOutputError` imported from `profiles.py` in both `deepagents_review.py` and `deepagents_fix.py`. ✓

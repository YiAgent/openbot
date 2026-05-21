# Evals Refactor Part 1: Middleware + Langsmith Consolidation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `evals/agents/middleware.py`, update baseline imports, canonicalize `inspect/langsmith.py`, redirect all callers.

**Architecture:** Merge convergence_middleware + structured_finalizer → middleware.py (zero inspect_ai imports). Move LangSmithExperiment to inspect layer. Update all callers before deleting old files (Part 2).

**Tech Stack:** Python, pytest, deepagents, langchain, inspect_ai, pydantic

**Spec:** `docs/superpowers/specs/2026-05-21-evals-refactor-design.md`
**Part 2:** `docs/superpowers/plans/2026-05-21-evals-refactor-part2.md`

---

## File Map (this part)

| Action | File |
|--------|------|
| **Create** | `evals/agents/middleware.py` |
| **Modify** | `evals/agents/baseline.py` |
| **Modify** | `evals/inspect/langsmith.py` |
| **Modify** | `tests/eval/test_convergence_middleware.py` |
| **Modify** | `tests/eval/test_structured_finalizer.py` |
| **Modify** | `tests/eval/test_langsmith_experiments.py` |
| **Modify** | `tests/eval/test_langsmith_routing.py` |
| **Modify** | `evals/tasks/review_martian.py` |
| **Modify** | `evals/tasks/fix_swe_bench_verified.py` |
| **Modify** | `evals/tasks/chat_swe_qa_pro.py` |
| **Modify** | `evals/tasks/test_swt_bench_verified.py` |
| **Modify** | `evals/scorers/swe_qa_pro.py` |
| **Modify** | `evals/scripts/writeback_swe_grades.py` |
| **Modify** | `evals/scripts/writeback_swt_grades.py` |

---

### Task 1: Create `evals/agents/middleware.py`

Merge `convergence_middleware.py` and `structured_finalizer.py` into one file. Zero `inspect_ai` imports.

**Files:**
- Create: `evals/agents/middleware.py`

- [ ] **Step 1: Write `evals/agents/middleware.py`**

```python
"""Deepagents middleware: convergence guards and structured-output finalizer."""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import BaseModel

from evals.agents.baseline import build_chat_model
from evals.common.termination import AgentTerminationError

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)

# ── Convergence guards ────────────────────────────────────────────────────────


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
        threshold: Times a signature must appear before interception. Default 3.
        window: How many recent calls to keep. Default 10.
        max_steers: Max interceptions per sample. Default 2.
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
            "ToolCallRepetitionGuard intercepted (name=%s, count=%d/%d, steers_left=%d)",
            request.tool_call.get("name"), current_count, self._threshold, self._steers_remaining,
        )
        return ToolMessage(
            content=self._STEER_MESSAGE.format(count=current_count),
            tool_call_id=request.tool_call.get("id", ""),
            name=request.tool_call.get("name", ""),
            status="error",
        )

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Any]) -> Any:
        i = self._intercept(request)
        return i if i is not None else handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]]) -> Any:
        i = self._intercept(request)
        return i if i is not None else await handler(request)


class ForceCommitBeforeBudget(AgentMiddleware):
    """Inject a wrap-up directive before the LLM-call budget runs out.

    Args:
        model_call_limit: Same cap passed to ``ModelCallLimitMiddleware``.
        commit_at_fraction: Fraction at which to fire. Default 0.7.
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

    def __init__(self, *, model_call_limit: int, commit_at_fraction: float = 0.7) -> None:
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
        self._calls_made += 1
        if self._steered or self._calls_made < self._commit_threshold:
            return None
        self._steered = True
        logger.info(
            "ForceCommitBeforeBudget injecting wrap-up directive (call %d/%d)",
            self._calls_made, self._model_call_limit,
        )
        return {"messages": [HumanMessage(content=self._STEER_MESSAGE.format(
            used=self._calls_made, limit=self._model_call_limit,
        ))]}

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
    """Build the standard convergence guard stack for one eval sample."""
    return [
        ToolCallRepetitionGuard(
            threshold=repetition_threshold,
            window=repetition_window,
            max_steers=repetition_max_steers,
        ),
        ForceCommitBeforeBudget(model_call_limit=model_call_limit, commit_at_fraction=commit_at_fraction),
    ]


# ── Structured-output finalizer ───────────────────────────────────────────────
#
# Agent runs WITHOUT response_format (thinking stays on, no forced-tool
# conflict). After ainvoke/invoke, finalize_structured makes one dedicated
# LLM call with tool_choice=forced + thinking=0, guaranteeing the schema.

_DEFAULT_PROSE_TAIL_CHARS = 12_000

_FINALIZER_PROMPT = """\
You just produced an analysis (shown below) but did NOT call the required
structured-response schema. Re-emit your answer now via the schema tool.
Use ONLY the content you already produced — do not invent new facts.

YOUR PREVIOUS ANALYSIS
----------------------
{prose}
"""

SchemaT = TypeVar("SchemaT", bound=BaseModel)


def _collect_prose(messages: list[BaseMessage]) -> str:
    parts: list[str] = []
    for msg in messages:
        if getattr(msg, "type", None) != "ai":
            continue
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            if content.strip():
                parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or ""
                    if isinstance(text, str) and text.strip():
                        parts.append(text)
    return "\n".join(parts)


def _truncate_prose(prose: str, max_chars: int) -> str:
    if len(prose) <= max_chars:
        return prose
    return "…[earlier analysis truncated]…\n" + prose[-max_chars:]


def _build_finalizer_runnable(model: str, schema: type[SchemaT]) -> Any:
    return build_chat_model(model, thinking_budget_tokens=0).with_structured_output(schema)


def _validate_finalizer_output(parsed: Any, schema: type[SchemaT]) -> SchemaT:
    if isinstance(parsed, schema):
        return parsed
    if isinstance(parsed, dict):
        try:
            return schema.model_validate(parsed)
        except Exception as exc:
            raise AgentTerminationError(
                f"structured finalizer returned dict that failed {schema.__name__} validation: {exc}"
            ) from exc
    raise AgentTerminationError(
        f"structured finalizer returned unexpected type {type(parsed).__name__}; expected {schema.__name__} or dict"
    )


def _prepare_prompt(messages: list[BaseMessage], prose_tail_chars: int) -> str:
    prose = _collect_prose(messages)
    if not prose.strip():
        raise AgentTerminationError(
            "structured finalizer received no agent prose — graph terminated before any AIMessage"
        )
    return _FINALIZER_PROMPT.format(prose=_truncate_prose(prose, prose_tail_chars))


def finalize_structured_sync(
    messages: list[BaseMessage], *, schema: type[SchemaT], model: str,
    prose_tail_chars: int = _DEFAULT_PROSE_TAIL_CHARS,
) -> SchemaT:
    """Synchronous structured-output finalizer (use inside active event loops)."""
    prompt = _prepare_prompt(messages, prose_tail_chars)
    try:
        parsed = _build_finalizer_runnable(model, schema).invoke(prompt)
    except Exception as exc:
        raise AgentTerminationError(f"structured finalizer call failed: {type(exc).__name__}: {exc}") from exc
    return _validate_finalizer_output(parsed, schema)


async def finalize_structured(
    messages: list[BaseMessage], *, schema: type[SchemaT], model: str,
    prose_tail_chars: int = _DEFAULT_PROSE_TAIL_CHARS,
) -> SchemaT:
    """Convert an agent's prose tail into a validated schema instance.

    One dedicated LLM call with ``tool_choice=forced`` + ``thinking=0``.
    """
    prompt = _prepare_prompt(messages, prose_tail_chars)
    try:
        parsed = await _build_finalizer_runnable(model, schema).ainvoke(prompt)
    except Exception as exc:
        raise AgentTerminationError(f"structured finalizer call failed: {type(exc).__name__}: {exc}") from exc
    return _validate_finalizer_output(parsed, schema)


class _StructuredFinalizerWrapper(Generic[SchemaT]):
    """Wraps a deepagents compiled graph to guarantee structured output.

    Intercepts ``ainvoke``/``invoke``, runs the agent without
    ``response_format``, then calls :func:`finalize_structured`.
    """

    def __init__(self, agent: Any, *, schema: type[SchemaT], model: str) -> None:
        self._agent = agent
        self._schema = schema
        self._model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)

    async def ainvoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> dict[str, Any]:
        fwd = {**kwargs, **({} if config is None else {"config": config})}
        ainvoke = getattr(self._agent, "ainvoke", None)
        if ainvoke is not None:
            result = await ainvoke(input, **fwd)
        else:
            import asyncio
            result = await asyncio.to_thread(self._agent.invoke, input, **fwd)
        return await self._finalize(result)

    def invoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> dict[str, Any]:
        fwd = {**kwargs, **({} if config is None else {"config": config})}
        result = self._agent.invoke(input, **fwd)
        messages = list(result.get("messages") or []) if isinstance(result, dict) else []
        existing = result.get("structured_response") if isinstance(result, dict) else None
        parsed = self._reuse_or_finalize_sync(existing, messages)
        if isinstance(result, dict):
            result["structured_response"] = parsed
        return result

    def _reuse_or_finalize_sync(self, existing: Any, messages: list[Any]) -> Any:
        if isinstance(existing, self._schema):
            return existing
        if isinstance(existing, dict):
            return self._schema.model_validate(existing)
        return finalize_structured_sync(messages, schema=self._schema, model=self._model)

    async def _finalize(self, result: Any) -> dict[str, Any]:
        messages = list(result.get("messages") or []) if isinstance(result, dict) else []
        existing = result.get("structured_response") if isinstance(result, dict) else None
        if isinstance(existing, self._schema):
            parsed: Any = existing
        elif isinstance(existing, dict):
            parsed = self._schema.model_validate(existing)
        else:
            parsed = await finalize_structured(messages, schema=self._schema, model=self._model)
        if isinstance(result, dict):
            result["structured_response"] = parsed
        return result


def wrap_agent_with_finalizer(agent: Any, *, schema: type[SchemaT], model: str) -> _StructuredFinalizerWrapper[SchemaT]:
    """Return a wrapper whose ``ainvoke``/``invoke`` guarantee structured output."""
    return _StructuredFinalizerWrapper(agent, schema=schema, model=model)


__all__ = [
    "AIMessage", "ForceCommitBeforeBudget", "ToolCallRepetitionGuard",
    "_StructuredFinalizerWrapper", "build_convergence_middlewares",
    "finalize_structured", "finalize_structured_sync", "wrap_agent_with_finalizer",
]
```

- [ ] **Step 2: Update `tests/eval/test_convergence_middleware.py`**

```python
# OLD
from evals.agents.convergence_middleware import (
    ForceCommitBeforeBudget, ToolCallRepetitionGuard, build_convergence_middlewares,
)
# NEW
from evals.agents.middleware import (
    ForceCommitBeforeBudget, ToolCallRepetitionGuard, build_convergence_middlewares,
)
```

- [ ] **Step 3: Update `tests/eval/test_structured_finalizer.py`**

```python
# OLD
from evals.agents.structured_finalizer import (
    _StructuredFinalizerWrapper, finalize_structured, finalize_structured_sync, wrap_agent_with_finalizer,
)
# NEW
from evals.agents.middleware import (
    _StructuredFinalizerWrapper, finalize_structured, finalize_structured_sync, wrap_agent_with_finalizer,
)
```

- [ ] **Step 4: Run middleware tests**

```bash
cd /Users/wy/projects/openbot
uv run pytest tests/eval/test_convergence_middleware.py tests/eval/test_structured_finalizer.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add evals/agents/middleware.py tests/eval/test_convergence_middleware.py tests/eval/test_structured_finalizer.py
git commit -m "refactor(evals): merge convergence_middleware + structured_finalizer → middleware.py"
```

---

### Task 2: Update `evals/agents/baseline.py` imports

**Files:**
- Modify: `evals/agents/baseline.py`

- [ ] **Step 1: Update lazy import in `build_budget_middlewares`**

```python
# OLD (inside build_budget_middlewares)
    from evals.agents.convergence_middleware import build_convergence_middlewares
# NEW
    from evals.agents.middleware import build_convergence_middlewares
```

- [ ] **Step 2: Update lazy import in `build_baseline_agent`**

```python
# OLD (inside build_baseline_agent)
    from evals.agents.structured_finalizer import wrap_agent_with_finalizer
# NEW
    from evals.agents.middleware import wrap_agent_with_finalizer
```

- [ ] **Step 3: Replace heavy comment in `build_baseline_agent`**

Remove the 10-line comment block starting with `# Structured output is enforced *after* the agent loop`. Replace with:

```python
    # Finalizer enforces structured output post-loop; see middleware.py.
```

- [ ] **Step 4: Trim `build_budget_middlewares` docstring**

Replace the long rationale docstring (20+ lines starting with `Four-layer defence...`) with:

```python
    """Standard budget + convergence middleware stack for one eval sample.

    Layers: ToolCallRepetitionGuard → ForceCommitBeforeBudget →
    ToolCallLimitMiddleware(continue) → ModelCallLimitMiddleware(end).
    """
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/eval/ -v -x
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add evals/agents/baseline.py
git commit -m "refactor(evals/baseline): update middleware imports, trim comments"
```

---

### Task 3: Canonicalize `evals/inspect/langsmith.py`

Inline `ensure_feedback_config` to remove the lazy import of the soon-to-be-deleted `langsmith_feedback.py`.

**Files:**
- Modify: `evals/inspect/langsmith.py`

- [ ] **Step 1: Add `ensure_feedback_config` after `logger = ...`**

```python
_RECONCILED_FEEDBACK_KEYS: set[str] = set()


def ensure_feedback_config(client: Any, key: str, config: dict[str, Any]) -> None:
    """Make LangSmith's stored config for ``key`` match ``config`` (idempotent)."""
    if key in _RECONCILED_FEEDBACK_KEYS:
        return
    try:
        try:
            client.create_feedback_config(feedback_key=key, feedback_config=config)
        except Exception:
            client.update_feedback_config(feedback_key=key, feedback_config=config)
        _RECONCILED_FEEDBACK_KEYS.add(key)
    except Exception as exc:
        logger.warning("langsmith: failed to reconcile feedback config for %r: %s", key, exc)


def reset_feedback_cache_for_tests() -> None:
    """Drop the per-process feedback reconciliation cache (test fixture)."""
    _RECONCILED_FEEDBACK_KEYS.clear()
```

- [ ] **Step 2: Remove lazy import in `_post_run`**

```python
# OLD
                if feedback_config is not None:
                    from evals.inspect.langsmith_feedback import ensure_feedback_config
                    ensure_feedback_config(client, feedback_key, feedback_config)

# NEW
                if feedback_config is not None:
                    ensure_feedback_config(client, feedback_key, feedback_config)
```

- [ ] **Step 3: Replace module docstring (first ~30 lines) with:**

```python
"""LangSmith trace routing and Experiment projection for Inspect evals.

``configure_tracing_for_dataset`` sets LangChain/LangSmith env vars.
``LangSmithExperiment`` projects Inspect sample scores into LangSmith
Experiment rows with per-sample scores tied back to dataset examples.
``ensure_feedback_config`` reconciles feedback key configs idempotently.
"""
```

- [ ] **Step 4: Commit (inspect/langsmith.py only)**

```bash
git add evals/inspect/langsmith.py
git commit -m "refactor(evals/inspect): inline ensure_feedback_config, trim docstring"
```

---

### Task 4: Redirect all callers to `evals.inspect.langsmith`

**Files:** tests + tasks + scorers + scripts (9 files, import line swaps only)

- [ ] **Step 1: `tests/eval/test_langsmith_experiments.py`**

```python
# OLD: from evals.agents.langsmith import LangSmithExperiment, _await_or_call
# NEW: from evals.inspect.langsmith import LangSmithExperiment, _await_or_call
```

- [ ] **Step 2: `tests/eval/test_langsmith_routing.py`**

```python
# OLD: from evals.agents import langsmith as ls
# NEW: from evals.inspect import langsmith as ls
```

- [ ] **Step 3–6: All 4 task files** (`review_martian.py`, `fix_swe_bench_verified.py`, `chat_swe_qa_pro.py`, `test_swt_bench_verified.py`)

In each file, replace:

```python
# OLD: from evals.agents.langsmith import LangSmithExperiment, configure_tracing_for_dataset
# NEW: from evals.inspect.langsmith import LangSmithExperiment, configure_tracing_for_dataset
```

- [ ] **Step 7: `evals/scorers/swe_qa_pro.py`**

```python
# OLD: from evals.agents.langsmith_feedback import ensure_feedback_config
# NEW: from evals.inspect.langsmith import ensure_feedback_config
```

- [ ] **Step 8–9: `evals/scripts/writeback_swe_grades.py` and `writeback_swt_grades.py`**

In each file, inside the function body:

```python
# OLD:     from evals.agents.langsmith_feedback import ensure_feedback_config
# NEW:     from evals.inspect.langsmith import ensure_feedback_config
```

- [ ] **Step 10: Run all eval tests**

```bash
uv run pytest tests/eval/ -v
```

Expected: all pass.

- [ ] **Step 11: Commit**

```bash
git add tests/eval/test_langsmith_experiments.py tests/eval/test_langsmith_routing.py \
        evals/tasks/ evals/scorers/swe_qa_pro.py \
        evals/scripts/writeback_swe_grades.py evals/scripts/writeback_swt_grades.py
git commit -m "refactor(evals): redirect agents.langsmith → inspect.langsmith everywhere"
```

---

**Part 1 complete. Continue with:** `docs/superpowers/plans/2026-05-21-evals-refactor-part2.md`

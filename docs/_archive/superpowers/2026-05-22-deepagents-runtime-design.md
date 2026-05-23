# DeepAgents Runtime — Base execution module plus task profiles

**Status:** design. Awaiting implementation plan.
**Date:** 2026-05-22
**Branch (proposed):** `feat/deepagents-runtime`
**PRD anchors:** §4.1 (triage), §4.2 (review), §4.3 (fix), §4.4 (chat), §4.5 (cost caps), §4.7 (cancellation), §5.1 (worker flow).
**Related specs:** `2026-05-21-unified-sandbox-entry-design.md`, `2026-05-21-agent-checkpoint-design.md`, `2026-05-21-queue-simplification-design.md`.

---

## Goal

Replace the current "each workflow builds its own DeepAgent" shape with a single
**Base DeepAgents runtime** and workflow-specific **AgentProfile** declarations.

The runtime owns shared execution behavior:

1. Model resolution, HTTP client construction (timeout / retries / token limits), and DeepAgents-compatible model normalization.
2. `create_deep_agent(...)` construction.
3. LangGraph checkpointer and `thread_id` wiring.
4. Standard run config: recursion limit, run metadata, tracing names.
5. Standard middleware: model-call limit, tool-call limit, convergence guards, budget hooks, cancellation hooks, and structured-output finalization.
6. Sandbox/backend injection without granting tools the profile did not request.
7. Consistent exception wrapping, telemetry, and cleanup.

Each task agent owns only the workflow-specific parts:

1. System prompt.
2. Input prompt construction.
3. Tool factory.
4. Structured response schema.
5. Domain result parsing.
6. Per-feature limits, model config, and sandbox requirement.

The target shape is:

```
workflow use case
    -> BaseDeepAgentRuntime.run(profile, request)
        -> build_chat_model(model, limits)
        -> create_deep_agent(model, tools, middleware, checkpointer, ...)
        -> agent.ainvoke(...)
        -> profile.parse_result(...)
    -> domain result
    -> workflow-specific GitHub write-back
```

The runtime does not create GitHub reviews, branches, commits, or PRs. Those stay
in the workflow use cases.

---

## Current problems

### Duplicate DeepAgents construction

`deepagents_chat.py`, `deepagents_review.py`, and `deepagents_fix.py` each call
`create_deep_agent(...)` directly. This spreads model normalization, run config,
checkpoint handling, structured output, and future middleware decisions across
multiple files.

The deletion test says the common module is justified: deleting it would force
the same execution complexity back into every workflow responder.

### No production middleware stack

The production responders call `create_deep_agent(...)` without any `middleware=`
argument. Only `_recursion_limit` and per-tool `ToolBudget` counters guard against
runaway agents. The eval baseline already runs a proper stack:
`ToolCallRepetitionGuard → ForceCommitBeforeBudget → ToolCallLimitMiddleware →
ModelCallLimitMiddleware`. Production should adopt the same discipline.

### No HTTP client configuration

Production passes a plain model-name string to `create_deep_agent`. LangChain then
constructs the httpx client with its own defaults — which on httpx means **no read
timeout**. A stalled provider socket will hang a worker thread indefinitely. The
eval baseline explicitly sets `timeout_s`, `max_retries`, and `max_output_tokens`
via `build_chat_model`. Production must do the same.

### Duplicate `ToolBudget` vs future middleware

`_review_tools.py` and `_fix_tools.py` each implement an independent `ToolBudget`
dataclass. When the runtime gains `ToolCallLimitMiddleware`, there will be two
simultaneous caps. The design must declare a single winner and a migration path.

### Checkpoint seam is only half connected

The worker can create and pass `agent_checkpointer` into `execute_handler`, and
review/fix responders accept `run_id` plus `checkpointer`, but the use cases do
not consistently pass `ctx.dispatch.run_id` and `ctx.agent_checkpointer` down to
the responder. A shared runtime makes checkpoint activation one rule instead of
one rule per responder.

### Public eval baseline is more coherent than production runtime

`evals/agents/baseline.py` already centralizes model resolution, timeout,
retry, output-token limits, budget middlewares, profile registration, structured
finalization, and LangSmith metadata for benchmark agents. Production should not
import the eval package, but the production runtime should adopt the same design
principle: one execution module, task-specific profiles.

### Tool safety lives in convention, not a single interface

Review is read-only, fix can write and execute, and chat currently has no tools.
That distinction is correct, but it is expressed by each responder directly
assembling its own tool list. A profile interface makes the safety rule explicit:
the only tools a task can use are the tools returned by its profile's tool
factory.

---

## Locked decisions

| Topic | Decision | Rationale |
|---|---|---|
| Runtime module | Add `openbot/infrastructure/agents/runtime.py` with `BaseDeepAgentRuntime`. | One deep module hides shared DeepAgents execution behavior behind a small interface. |
| Profile module | Add `openbot/infrastructure/agents/profiles.py` with `AgentProfile` and `AgentRequest`. | Profiles are the seam where task-specific behavior varies. |
| Model names module | Add `openbot/infrastructure/agents/model_names.py` with `normalize_for_langchain()` and `display_name()`. | Normalization and display are two distinct transforms; both live in one place. |
| HTTP client | Runtime constructs a `BaseChatModel` via `build_agent_chat_model()` before passing it to `create_deep_agent`. Never pass a raw model string in production. | Explicit timeout/retry/max_tokens prevent silent hangs and truncated thinking output. |
| Middleware stack | Runtime builds: `ToolCallRepetitionGuard → ToolCallLimitMiddleware → ModelCallLimitMiddleware`. Profile may supply `extra_middleware` appended after the standard stack. | Matches eval baseline discipline; profiles cannot bypass or replace safety middleware. |
| `ToolBudget` retirement | Retire the hand-rolled `ToolBudget` in `_review_tools.py` and `_fix_tools.py` in Phase 5. `ToolCallLimitMiddleware` takes over the budget role. | Single cap avoids races and double-counting. Until middleware lands in Phase 1–3, existing `ToolBudget` stays active. |
| Workflow ownership | Use cases keep all GitHub write-back and business branching. | Agent execution and product actions are separate concerns. |
| Tool safety | Tools are granted only by `profile.build_tools(request)`. | Physical capability control is tool registration, not prompt wording. |
| Sandbox | Runtime accepts sandbox/backend context, but the profile decides whether sandbox is required, optional, or forbidden. | Base supports sandbox; profiles define capability. |
| Checkpoint | Runtime activates checkpoint only when `request.run_id` and `request.checkpointer` are both present and the profile allows it. | Prevents LangGraph config errors and keeps dev/test graceful. |
| Structured output | Runtime always returns `profile.parse_result(raw_result)`. | Domain objects remain the workflow interface; Pydantic/LangGraph shapes stay inside infrastructure. |
| Eval import direction | Production runtime must not import `evals.*`. | Eval remains a comparator layer, not a production dependency. |
| HarnessProfile | Production runtime registers `HarnessProfileConfig(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))` for every model on first use. | Prevents the auto-attached `task` delegation tool from branching single-objective runs. |
| First migration | Migrate review first, then fix, then chat. | Review has the smallest blast radius; fix validates sandbox/checkpoint; chat can clean up last. |
| Triage | Define the profile interface so triage can be added later, but do not implement a triage DeepAgent in this slice. | Avoid expanding scope before the current responder set is consolidated. |

---

## Architecture

### Module map

```
openbot/
  infrastructure/
    agents/
      runtime.py                 # NEW: BaseDeepAgentRuntime + build_agent_chat_model
      profiles.py                # NEW: AgentProfile, AgentRequest, AgentRunLimits
      model_names.py             # NEW: normalize_for_langchain(), display_name()
      deepagents_review.py       # SHRINKS: profile declaration + compatibility wrapper
      deepagents_fix.py          # SHRINKS: profile declaration + compatibility wrapper
      deepagents_chat.py         # SHRINKS: profile declaration + compatibility wrapper
      _review_tools.py           # KEEP (Phase 5: retire ToolBudget)
      _fix_tools.py              # KEEP (Phase 5: retire ToolBudget)
      _review_schema.py          # KEEP: schema + domain parsing helpers
      _fix_schema.py             # KEEP: schema + domain parsing helpers
```

The compatibility wrappers keep the current public classes
`DeepAgentsReviewResponder`, `DeepAgentsFixResponder`, and
`DeepAgentsChatResponder` during migration. Internally they delegate to
`BaseDeepAgentRuntime.run(...)`.

### `model_names.py` interface

```python
def normalize_for_langchain(model: str) -> str:
    """Map provider/name (LiteLLM) → provider:name (langchain_litellm).

    anthropic/GLM-5.1 → anthropic:GLM-5.1
    anthropic:GLM-5.1 → anthropic:GLM-5.1  (idempotent)
    GLM-5.1           → GLM-5.1             (bare names untouched)
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
    GLM-5.1           → GLM-5.1  (idempotent)

    Only the first segment is stripped — provider:org:model → org:model.
    """
    if ":" not in model:
        return model
    return model.split(":", 1)[1]
```

### Runtime interface

```python
@dataclass(frozen=True, slots=True)
class AgentRunLimits:
    recursion_limit: int
    # Middleware caps
    model_call_limit: int | None = None
    tool_call_limit: int | None = None
    # HTTP client knobs — passed to build_agent_chat_model
    wall_seconds: int | None = None      # asyncio.wait_for envelope
    model_timeout_s: int | None = None   # per-request httpx timeout
    max_retries: int | None = None       # HTTP-layer retries on 429/5xx
    max_output_tokens: int | None = None # provider-side max_tokens
    thinking_budget_tokens: int = 0      # 0 = disabled


@dataclass(frozen=True, slots=True)
class AgentRequest:
    event: UnifiedEvent
    input: Mapping[str, Any]       # profile-specific; see "Per-profile input schemas"
    adapter: ChannelAdapterPort | None = None
    sandbox: SandboxPort | None = None
    run_id: str | None = None
    checkpointer: BaseCheckpointSaver | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SandboxRequirement(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    FORBIDDEN = "forbidden"


class AgentProfile(Protocol[DomainResult]):
    feature: Feature
    agent_name: str               # "review" | "fix" | "chat" — used in traces
    response_schema: type[BaseModel] | None   # Pydantic class, not a dict schema
    limits: AgentRunLimits
    sandbox_requirement: SandboxRequirement
    checkpoint_enabled: bool      # True = profile SUPPORTS checkpointing
    extra_middleware: Sequence[AgentMiddleware]  # appended after standard stack

    def system_prompt(self, request: AgentRequest) -> str: ...
    def user_message(self, request: AgentRequest) -> str: ...
    def build_tools(self, request: AgentRequest) -> Sequence[BaseTool]: ...
    def parse_result(self, result: Mapping[str, Any]) -> DomainResult: ...
```

**Notes on `AgentProfile`:**
- `checkpoint_enabled` means "this profile CAN use checkpointing when the runtime also has a `run_id` and `checkpointer`". A `True` value is necessary but not sufficient — the runtime always gates on all three.
- `response_schema` must be a Pydantic `BaseModel` subclass, not a `dict` or `AutoStrategy`. The structured-output finalizer requires a concrete class.
- `extra_middleware` is for per-profile observability shims only. Profiles must not use it to replace or bypass the standard safety stack.

The runtime interface:

```python
class BaseDeepAgentRuntime:
    async def run(
        self,
        profile: AgentProfile[DomainResult],
        request: AgentRequest,
    ) -> DomainResult:
        ...
```

### Per-profile input schemas

`AgentRequest.input` is `Mapping[str, Any]` for flexibility, but each profile
has a documented schema. The profile's `user_message(request)` is the canonical
consumer; other code should not peek into `input` directly.

| Profile | Expected `input` keys | Notes |
|---|---|---|
| Review | `{"diff": str}` | Pre-fetched by compatibility wrapper via `adapter.get_pr_diff`. Truncation happens inside the profile's `user_message`. |
| Fix | `{"issue_title": str, "issue_body": str, "base_sha": str}` | Sourced from `issue` dict returned by `ChannelAdapterPort.get_issue`. |
| Chat | `{"user_request": str}` | The raw mention body after stripping the `@openbot` trigger. |

### HTTP client construction

The runtime calls `build_agent_chat_model` (defined in `runtime.py`) before
constructing the agent. This mirrors `evals/agents/baseline.py`'s `build_chat_model`
but without the `thinking` support for the first production cut.

```python
def build_agent_chat_model(
    model: str,          # must already be in provider:name form
    limits: AgentRunLimits,
) -> BaseChatModel:
    """Construct a LangChain chat model with explicit HTTP client configuration.

    Never passes a bare model string to create_deep_agent — the httpx default
    has no read timeout, which means a stalled provider socket hangs the worker.
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
```

### Middleware stack

The runtime builds the standard stack in order:

```python
def _build_standard_middleware(limits: AgentRunLimits) -> list[AgentMiddleware]:
    stack: list[AgentMiddleware] = [ToolCallRepetitionGuard()]
    if limits.tool_call_limit is not None:
        stack.append(ToolCallLimitMiddleware(
            thread_limit=limits.tool_call_limit,
            exit_behavior="continue",  # let model emit final answer
        ))
    if limits.model_call_limit is not None:
        stack.append(ModelCallLimitMiddleware(
            thread_limit=limits.model_call_limit,
            exit_behavior="end",
        ))
    return stack
```

**Ordering rule:** Standard safety middleware always runs first. Profile
`extra_middleware` is appended at the end. Profiles cannot reorder or omit
the standard stack.

**`ToolBudget` coexistence (Phase 1–3):** Until `ToolBudget` is retired in
Phase 5, both caps run simultaneously. In practice `ToolBudget` trips first
because its budget (5 for review, 20 for fix) is lower than the middleware
`tool_call_limit` that will replace it. Verify in tests that the middleware
limit is set at or above the `ToolBudget` value during the transition.

### HarnessProfile registration

The runtime calls `_register_profile(model)` before each `create_deep_agent`:

```python
_REGISTERED_MODELS: set[str] = set()

def _register_profile(model: str) -> None:
    if model in _REGISTERED_MODELS:
        return
    register_harness_profile(
        model,
        HarnessProfileConfig(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    _REGISTERED_MODELS.add(model)
```

This prevents the auto-attached `task` delegation tool from making single-objective
production runs branch into self-delegation. The registration is global and
idempotent.

### Runtime flow

```
BaseDeepAgentRuntime.run(profile, request)
    ↓
validate request against profile.sandbox_requirement
    ↓
resolve model from profile.feature via primary_model_for(...)
    ↓
model = normalize_for_langchain(raw_model)  # provider/name → provider:name
    ↓
register HarnessProfile for model (idempotent)
    ↓
chat_model = build_agent_chat_model(model, profile.limits)
    ↓
build standard middleware list from profile.limits
    ↓
tools = profile.build_tools(request)
    ↓
effective_checkpointer = request.checkpointer
    only if profile.checkpoint_enabled and request.run_id is present
    ↓
agent = create_deep_agent(
    model=chat_model,          # BaseChatModel, not a string
    tools=tools,
    system_prompt=profile.system_prompt(request),
    response_format=profile.response_schema,
    middleware=standard_middleware + list(profile.extra_middleware),
    checkpointer=effective_checkpointer,
)
    ↓
config = RunnableConfig(
    recursion_limit=profile.limits.recursion_limit,
    metadata={
        "feature": profile.feature.value,
        "agent_profile": profile.agent_name,
        "run_id": request.run_id,
        "task_id": request.event.delivery_id,
        "repo": request.event.repo,
        "actor": request.event.actor,
        "model": display_name(model),      # strip provider: prefix
        "checkpoint_enabled": effective_checkpointer is not None,
        "sandbox_present": request.sandbox is not None,
        **request.metadata,
    },
)
if effective_checkpointer:
    config["configurable"] = {"thread_id": request.run_id}
    ↓
invoke_coro = agent.ainvoke(
    {"messages": [{"role": "user", "content": profile.user_message(request)}]},
    config=config,
)
if profile.limits.wall_seconds:
    raw = await asyncio.wait_for(invoke_coro, timeout=profile.limits.wall_seconds)
else:
    raw = await invoke_coro
    ↓
return profile.parse_result(raw)
```

### Error taxonomy

| Error class | Raised when |
|---|---|
| `AgentSandboxRequiredError` | Profile requires sandbox but `request.sandbox is None`. |
| `AgentSandboxForbiddenError` | Profile forbids sandbox but request includes one. |
| `AgentStructuredOutputError` | `parse_result` cannot find or coerce the structured response. |
| `AgentBudgetExhaustedError` | Agent terminates via middleware budget limit before producing a result. Wraps `AgentTerminationError` from the middleware layer. |
| `AgentTimeoutError` | `asyncio.wait_for` fires on `wall_seconds` limit. |
| `AgentExecutionError` | Any other DeepAgents/LangGraph exception before a result is produced. |

Workflow use cases convert these into existing fallback comments and audit
outcomes. They must not expose raw LangChain or DeepAgents exception text to
GitHub users.

---

## Profiles

### Review profile

**Purpose:** Read a PR diff and produce `ReviewFindings`.

| Field | Value |
|---|---|
| `feature` | `Feature.REVIEW` |
| `agent_name` | `"review"` |
| `sandbox_requirement` | `OPTIONAL` in this slice |
| `checkpoint_enabled` | `True` |
| `response_schema` | `ReviewFindingsSchema` |
| tools | `read_file`, `grep_repo` from `_review_tools.py` |
| parser | existing `parse_structured_response(...) -> ReviewFindings` |
| `limits.recursion_limit` | `25` |
| `limits.tool_call_limit` | `8` (slightly above current ToolBudget=5 for transition) |
| `limits.model_call_limit` | `10` |
| `limits.model_timeout_s` | `120` |
| `limits.max_retries` | `2` |
| `limits.max_output_tokens` | `16_384` |

Review remains read-only. The diff is fetched by the compatibility wrapper
before constructing `AgentRequest`, not inside the profile.

### Fix profile

**Purpose:** Use a cloned sandbox to produce a tested `FixOutcome`.

| Field | Value |
|---|---|
| `feature` | `Feature.FIX` |
| `agent_name` | `"fix"` |
| `sandbox_requirement` | `REQUIRED` |
| `checkpoint_enabled` | `True` |
| `response_schema` | `FixOutcomeSchema` |
| tools | `read_file`, `write_file`, `list_files`, `run_command`, `git_diff`, `search_files` from `_fix_tools.py` |
| parser | existing `parse_structured_response(...) -> FixOutcome` |
| `limits.recursion_limit` | `60` |
| `limits.tool_call_limit` | `25` (slightly above current ToolBudget=20 for transition) |
| `limits.model_call_limit` | `20` |
| `limits.model_timeout_s` | `300` |
| `limits.max_retries` | `2` |
| `limits.max_output_tokens` | `16_384` |
| `limits.wall_seconds` | `1800` (30-minute ceiling) |

The runtime only runs the agent. The use case still owns:

1. Fetching the issue.
2. Branch name selection.
3. `adapter.create_branch(...)`.
4. `sandbox.commit_and_push(...)`.
5. `adapter.open_pull_request(...)`.
6. Success/failure comments.

### Chat profile

**Purpose:** Answer `@openbot` requests from the context currently available.

| Field | Value |
|---|---|
| `feature` | `Feature.CHAT` |
| `agent_name` | `"chat"` |
| `sandbox_requirement` | `FORBIDDEN` initially |
| `checkpoint_enabled` | `False` initially |
| `response_schema` | `None` |
| tools | none initially |
| parser | extract final assistant text |
| `limits.recursion_limit` | `10` |
| `limits.model_call_limit` | `3` |
| `limits.model_timeout_s` | `60` |
| `limits.max_output_tokens` | `4_096` |

**Note:** Chat currently passes `config = {}` (no recursion limit) and
`config or None` (LangGraph sees `None`). This is a bug — LangGraph falls
back to a default that can be very high. The runtime fixes this by always
setting `recursion_limit` from the profile.

This preserves the current lightweight chat behavior. Tool-using chat can be a
later profile change after sandbox/read-only tool policy is finalized.

### Triage profile

Triage is a reserved profile, not implemented in this slice. The interface
must support it without adding new runtime concepts:

| Field | Likely value later |
|---|---|
| `feature` | `Feature.TRIAGE` |
| `sandbox_requirement` | `OPTIONAL` or `REQUIRED`, depending on classifier output |
| tools | read-only repo tools plus limited test/repro runner |
| output | domain triage result with labels, priority, repro evidence |

---

## Public responder compatibility

Existing use cases should keep their current responder class imports during the
first migration. The compatibility wrapper fetches inputs (diff, issue) before
constructing the `AgentRequest` so that profiles remain pure domain logic.

```python
class DeepAgentsReviewResponder:
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
        # Diff fetching stays in the wrapper; profiles are pure.
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
```

```python
class DeepAgentsFixResponder:
    def __init__(self, runtime: BaseDeepAgentRuntime | None = None) -> None:
        self._runtime = runtime or BaseDeepAgentRuntime()

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
```

The wrapper class remains the workflow-facing seam until the use cases are ready
to call `BaseDeepAgentRuntime` directly. This keeps the migration small and
preserves test fixtures that monkeypatch `_RESPONDER`.

---

## Checkpoint and cancellation

### Checkpoint activation

The runtime activates checkpointing only when all conditions are true:

1. `profile.checkpoint_enabled is True`.
2. `request.run_id` is not empty.
3. `request.checkpointer` is not `None`.

When active:

```python
create_deep_agent(..., checkpointer=request.checkpointer)
config["configurable"] = {"thread_id": request.run_id}
```

When inactive, the runtime passes no checkpointer and no `configurable.thread_id`.

### Checkpoint cleanup

Checkpoint cleanup belongs to the worker/use-case completion path, not the
profile. The runtime does not know whether a domain result has been fully
committed to GitHub. For example, fix agent success is not complete until the
branch is pushed and the PR is opened or a failure comment is posted.

Implementation plan must add cleanup at stable terminal states:

1. Review posted successfully.
2. Review fallback posted after agent failure.
3. Fix PR opened.
4. Fix tests-failed comment posted.
5. Fix fallback comment posted after agent/branch/push/open-PR failure.
6. Queue entry moved to DLQ.

**Cleanup API (proposed):** The use case calls a `checkpoint_cleanup(run_id, checkpointer)` helper after confirming the terminal state. The helper deletes the LangGraph thread. This is a one-liner utility, not a runtime concern.

### Cancellation

The runtime should expose a middleware hook for cancellation, but business-level
checkpoints still belong around slow workflow operations:

1. Before and after agent invocation.
2. After sandbox clone.
3. Before branch creation.
4. Before push.
5. Before PR open.

This keeps cancellation responsive even when the agent itself is not currently
inside a LangGraph node that checks cancellation. The `wall_seconds` timeout
(enforced via `asyncio.wait_for`) provides a hard ceiling for hung agents even
when the cancellation signal does not propagate cleanly.

---

## Sandbox and tools

Sandbox is a capability source, not a runtime default. The runtime may receive a
`SandboxPort`, but no task can use it unless its profile builds sandbox-backed
tools.

| Profile | Sandbox requirement | Writable tools |
|---|---|---|
| Review | Optional | No |
| Fix | Required | Yes |
| Chat | Forbidden initially | No |
| Triage | Deferred | Deferred |

The safety invariant:

> A workflow cannot perform a side effect unless its profile registers a tool
> that performs that side effect.

Prompt text is not a safety mechanism. It can explain policy to the model, but
the enforceable mechanism is the tool list.

---

## Middleware

### Standard middleware stack (in order)

| Middleware | Purpose |
|---|---|
| `ToolCallRepetitionGuard` | Short-circuit identical repeated tool calls. |
| `ToolCallLimitMiddleware(exit_behavior="continue")` | Cap total tool invocations; let model emit final answer after. |
| `ModelCallLimitMiddleware(exit_behavior="end")` | Hard-stop after model call cap; agent cannot continue. |

Budget and cancellation middleware can land behind the same runtime seam once
their production ports are ready. The interface should be designed now so adding
them does not change profile callers.

### `ToolBudget` retirement plan

| Phase | State |
|---|---|
| Phase 1–3 | `ToolBudget` stays in tool closures. `ToolCallLimitMiddleware` limits are set ≥ ToolBudget values to avoid double-tripping. |
| Phase 4 | Chat has no tools; no retirement needed there. |
| Phase 5 | Remove `ToolBudget` from `_review_tools.py` and `_fix_tools.py`. Set final middleware limits to the retired budget values (5 for review, 20 for fix). |

### Ordering rule

Middleware order is runtime-owned and tested. Profiles may supply extra
middleware only through an explicit `extra_middleware` field, appended after
standard safety middleware. Profiles must not replace the standard stack.

---

## Observability

Every runtime invocation should attach stable metadata to the `RunnableConfig`:

| Field | Source |
|---|---|
| `feature` | `profile.feature.value` |
| `agent_profile` | `profile.agent_name` |
| `run_id` | `request.run_id` |
| `task_id` | `request.event.delivery_id` or dispatch metadata when supplied |
| `repo` | `request.event.repo` |
| `actor` | `request.event.actor` |
| `model` | `display_name(model)` — strips `provider:` prefix for LangSmith readability |
| `checkpoint_enabled` | runtime decision (`effective_checkpointer is not None`) |
| `sandbox_present` | `request.sandbox is not None` |

Sentry AI monitoring should set conversation id from `run_id` when present, then
fall back to `event.resource_key`, then `delivery_id`. Prompt/response capture
remains governed by existing Sentry PII settings.

---

## Error handling

Runtime errors are infrastructure errors. Use cases decide user-facing text.

| Runtime outcome | Use-case behavior |
|---|---|
| Review agent succeeds | Filter findings and create PR review. |
| Review agent fails | Post existing review fallback comment/review. |
| Fix agent succeeds with tests passing | Create branch, push, open PR. |
| Fix agent succeeds with tests failing | Post tests-failed comment. |
| Fix agent fails | Post existing agent-failed comment. |
| Chat agent succeeds | Reply with text. |
| Chat agent fails | Reply with existing chat error template. |

The runtime must log full exception context (including budget exhaustion details
and timeout durations), but workflow comments must stay stable and non-sensitive.

---

## Testing strategy

### Runtime unit tests

Add tests under `tests/infrastructure/agents/test_runtime.py`:

1. Builds `BaseChatModel` via `build_agent_chat_model` — never passes a raw string to `create_deep_agent`.
2. Passes normalized model name (`provider:name` form) to `build_agent_chat_model`.
3. Passes `response_format` only when profile defines a schema.
4. Adds `configurable.thread_id` only when `run_id` and `checkpointer` are both present and `profile.checkpoint_enabled` is `True`.
5. Does not add checkpoint config when any part is missing.
6. Enforces `REQUIRED`, `OPTIONAL`, and `FORBIDDEN` sandbox requirements.
7. Calls `profile.parse_result(...)` and returns the domain object.
8. Wraps DeepAgents exceptions in stable runtime errors (`AgentExecutionError`).
9. Wraps `asyncio.TimeoutError` in `AgentTimeoutError` when `wall_seconds` is set.
10. Applies `wall_seconds` via `asyncio.wait_for` when present on limits.
11. Registers HarnessProfile exactly once per model (idempotent).
12. Includes all observability metadata in `RunnableConfig` (`model` uses `display_name` not raw string).

### `model_names.py` unit tests

Add under `tests/infrastructure/agents/test_model_names.py`:

1. `normalize_for_langchain("anthropic/GLM-5.1")` → `"anthropic:GLM-5.1"`.
2. `normalize_for_langchain("anthropic:GLM-5.1")` → `"anthropic:GLM-5.1"` (idempotent).
3. `normalize_for_langchain("GLM-5.1")` → `"GLM-5.1"` (bare untouched).
4. `display_name("anthropic:GLM-5.1")` → `"GLM-5.1"`.
5. `display_name("GLM-5.1")` → `"GLM-5.1"` (idempotent).

### Profile migration tests

Keep existing responder tests and update expectations to assert delegation:

1. Review profile builds read-only tools and returns `ReviewFindings`.
2. Fix profile requires sandbox and returns `FixOutcome`.
3. Chat profile extracts text and registers no tools.
4. Chat profile sets `recursion_limit` (verifies the current None/empty-dict bug is fixed).
5. No test makes a real network or model call; `create_deep_agent` stays monkeypatched.

### Use-case tests

Add/repair use-case tests for the currently missing pass-through:

1. `maybe_run_review` passes `ctx.dispatch.run_id` and `ctx.agent_checkpointer`.
2. `maybe_run_fix` passes `ctx.dispatch.run_id` and `ctx.agent_checkpointer`.
3. Fix with missing sandbox still posts `_NO_SANDBOX` and never invokes runtime.
4. Review/fix terminal states call checkpoint cleanup when cleanup is implemented.

### Regression command

Fast gate:

```bash
uv run pytest tests/infrastructure/agents tests/application/use_cases -q
```

Broader gate:

```bash
uv run pytest tests/eval tests/infrastructure/agents tests/application -q
```

---

## Migration plan

### Phase 1: Runtime skeleton

1. Add `model_names.py` with `normalize_for_langchain` and `display_name`.
2. Add `profiles.py` with `AgentProfile`, `AgentRequest`, `AgentRunLimits`, `SandboxRequirement`.
3. Add `runtime.py` with `BaseDeepAgentRuntime` and `build_agent_chat_model`.
4. Add `model_names` and `runtime` unit tests with fake profiles and monkeypatched `create_deep_agent`.

No workflow behavior changes in this phase.

### Phase 2: Review migration

1. Convert review prompt/tool/schema/parser into `ReviewProfile` (with limits table above).
2. Keep `DeepAgentsReviewResponder.review_for_event(...)` as compatibility seam; move diff fetch into wrapper.
3. Update review use case to pass `run_id` and `agent_checkpointer`.
4. Run review responder and review use-case tests.
5. Verify `ToolCallLimitMiddleware` limit (8) does not conflict with `ToolBudget` (5).

### Phase 3: Fix migration

1. Convert fix prompt/tool/schema/parser into `FixProfile` (with limits table above).
2. Keep `DeepAgentsFixResponder.fix_for_event(...)` as compatibility seam.
3. Update fix use case to pass `run_id` and `agent_checkpointer`.
4. Confirm missing sandbox path still degrades before runtime invocation.
5. Verify `ToolCallLimitMiddleware` limit (25) does not conflict with `ToolBudget` (20).
6. Verify `wall_seconds=1800` ceiling is enforced via `asyncio.wait_for`.

### Phase 4: Chat migration

1. Convert chat prompt/text parser into `ChatProfile` (with limits table above — critically, `recursion_limit=10`).
2. Remove stale "ACK only" wording from code comments and GitHub help copy if behavior is now real freeform reply.
3. Keep chat tool list empty until a separate read-only chat tools design lands.

### Phase 5: Cleanup and `ToolBudget` retirement

1. Remove `ToolBudget` from `_review_tools.py` and set `ToolCallLimitMiddleware` limit to 5 in `ReviewProfile`.
2. Remove `ToolBudget` from `_fix_tools.py` and set `ToolCallLimitMiddleware` limit to 20 in `FixProfile`.
3. Delete duplicated `_normalize_model_name` helpers from responder files (now in `model_names.py`).
4. Document the runtime/profile pattern in `openbot/infrastructure/agents/__init__.py` or a short `docs/architecture` note.
5. Update eval docs only if production/eval vocabulary changes. `deepagents_baseline` remains the eval comparator.

---

## Non-goals

This design does not:

1. Replace the queue architecture.
2. Implement the pending queue simplification design.
3. Implement triage DeepAgent behavior.
4. Add tool-using chat.
5. Merge production agents with `evals/agents/baseline.py`.
6. Move GitHub branch/PR/review creation into the runtime.
7. Add a plugin marketplace or dynamic profile loading.
8. Add extended thinking support (thinking_budget_tokens defaults to 0; re-enable when production gateway confirms support).

---

## Acceptance criteria

The implementation is complete when:

1. Review, fix, and chat all execute through `BaseDeepAgentRuntime`.
2. Use cases no longer call `create_deep_agent` indirectly through bespoke construction code; only runtime does.
3. `build_agent_chat_model` is always called; no profile passes a bare string model name to `create_deep_agent`.
4. Review/fix pass `run_id` and `agent_checkpointer` into the runtime path.
5. Review remains read-only and fix remains the only migrated profile with write/execute tools.
6. Existing domain return types are unchanged: `ReviewFindings`, `FixOutcome`, and `str` for chat.
7. Existing responder class names still import successfully.
8. Unit tests cover runtime checkpoint activation, sandbox requirement enforcement, structured result parsing, wall-seconds timeout, and budget exhaustion wrapping.
9. Chat profile always receives a `recursion_limit` (the current empty-config bug is gone).
10. `ToolBudget` is retired from both tool modules by Phase 5 end.
11. No production module imports from `evals.*`.
12. All observability metadata fields (including `display_name`-stripped model) appear in every `RunnableConfig`.

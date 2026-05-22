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

1. Model resolution and DeepAgents-compatible model normalization.
2. `create_deep_agent(...)` construction.
3. LangGraph checkpointer and `thread_id` wiring.
4. Standard run config: recursion limit, run metadata, tracing names.
5. Standard middleware: model/tool limits, convergence guards, budget hooks,
   cancellation hooks, and structured-output finalization.
6. Sandbox/backend injection without granting tools the profile did not request.
7. Consistent exception wrapping, telemetry, and cleanup.

Each task agent owns only the workflow-specific parts:

1. System prompt.
2. Input prompt construction.
3. Tool factory.
4. Structured response schema.
5. Domain result parsing.
6. Per-feature limits and sandbox requirement.

The target shape is:

```
workflow use case
    -> BaseDeepAgentRuntime.run(profile, request)
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
| Workflow ownership | Use cases keep all GitHub write-back and business branching. | Agent execution and product actions are separate concerns. |
| Tool safety | Tools are granted only by `profile.build_tools(request)`. | Physical capability control is tool registration, not prompt wording. |
| Sandbox | Runtime accepts sandbox/backend context, but the profile decides whether sandbox is required, optional, or forbidden. | Base supports sandbox; profiles define capability. |
| Checkpoint | Runtime activates checkpoint only when `request.run_id` and `request.checkpointer` are both present and the profile allows it. | Prevents LangGraph config errors and keeps dev/test graceful. |
| Structured output | Runtime always returns `profile.parse_result(raw_result)`. | Domain objects remain the workflow interface; Pydantic/LangGraph shapes stay inside infrastructure. |
| Eval import direction | Production runtime must not import `evals.*`. | Eval remains a comparator layer, not a production dependency. |
| First migration | Migrate review first, then fix, then chat. | Review has the smallest blast radius; fix validates sandbox/checkpoint; chat can clean up docstring/copy drift last. |
| Triage | Define the profile interface so triage can be added later, but do not implement a triage DeepAgent in this slice. | Avoid expanding scope before the current responder set is consolidated. |

---

## Architecture

### Module map

```
openbot/
  infrastructure/
    agents/
      runtime.py                 # NEW: BaseDeepAgentRuntime
      profiles.py                # NEW: AgentProfile, AgentRequest, AgentRunLimits
      model_names.py             # NEW: shared model normalization helper
      deepagents_review.py       # SHRINKS: profile declaration + compatibility wrapper
      deepagents_fix.py          # SHRINKS: profile declaration + compatibility wrapper
      deepagents_chat.py         # SHRINKS: profile declaration + compatibility wrapper
      _review_tools.py           # KEEP: review read-only tool factory
      _fix_tools.py              # KEEP: fix sandbox tool factory
      _review_schema.py          # KEEP: schema + domain parsing helpers
      _fix_schema.py             # KEEP: schema + domain parsing helpers
```

The compatibility wrappers keep the current public classes
`DeepAgentsReviewResponder`, `DeepAgentsFixResponder`, and
`DeepAgentsChatResponder` during migration. Internally they delegate to
`BaseDeepAgentRuntime.run(...)`.

### Runtime interface

```python
@dataclass(frozen=True, slots=True)
class AgentRunLimits:
    recursion_limit: int
    model_call_limit: int | None = None
    tool_call_limit: int | None = None
    wall_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class AgentRequest:
    event: UnifiedEvent
    input: Mapping[str, Any]
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
    name: str
    response_schema: type[Any] | None
    limits: AgentRunLimits
    sandbox_requirement: SandboxRequirement
    checkpoint_enabled: bool

    def system_prompt(self, request: AgentRequest) -> str: ...
    def user_message(self, request: AgentRequest) -> str: ...
    def build_tools(self, request: AgentRequest) -> Sequence[BaseTool]: ...
    def parse_result(self, result: Mapping[str, Any]) -> DomainResult: ...
```

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

### Runtime flow

```
BaseDeepAgentRuntime.run(profile, request)
    ↓
validate request against profile.sandbox_requirement
    ↓
resolve model from profile.feature via primary_model_for(...)
    ↓
normalize model name for DeepAgents/LangChain
    ↓
build standard middleware list from profile.limits
    ↓
tools = profile.build_tools(request)
    ↓
effective_checkpointer = request.checkpointer
    only if profile.checkpoint_enabled and request.run_id is present
    ↓
agent = create_deep_agent(
    model=model,
    tools=tools,
    system_prompt=profile.system_prompt(request),
    response_format=profile.response_schema,
    middleware=standard_middleware,
    checkpointer=effective_checkpointer,
)
    ↓
config = {
    "recursion_limit": profile.limits.recursion_limit,
    "metadata": request.metadata + runtime metadata,
}
if effective_checkpointer:
    config["configurable"] = {"thread_id": request.run_id}
    ↓
raw = await agent.ainvoke({"messages": [{"role": "user", "content": ...}]}, config=config)
    ↓
return profile.parse_result(raw)
```

The runtime raises stable infrastructure-level errors:

| Error | Raised when |
|---|---|
| `AgentSandboxRequiredError` | Profile requires sandbox but `request.sandbox is None`. |
| `AgentSandboxForbiddenError` | Profile forbids sandbox but request includes one. |
| `AgentStructuredOutputError` | `parse_result` cannot find or coerce the structured response. |
| `AgentExecutionError` | DeepAgents invocation raises before producing a result. |

Workflow use cases convert these into existing fallback comments and audit
outcomes. They should not expose raw LangChain or DeepAgents exception text to
GitHub users.

---

## Profiles

### Review profile

**Purpose:** Read a PR diff and produce `ReviewFindings`.

| Field | Value |
|---|---|
| `feature` | `Feature.REVIEW` |
| `name` | `review` |
| `sandbox_requirement` | `OPTIONAL` in this slice |
| `checkpoint_enabled` | `True` |
| `response_schema` | `ReviewFindingsSchema` |
| tools | `read_file`, `grep_repo` from `_review_tools.py` |
| parser | existing `parse_structured_response(...) -> ReviewFindings` |

Review remains read-only. If a sandbox is present in the future, the profile may
switch from GitHub API read tools to sandbox read tools, but that is a later
optimization. The first migration preserves current behavior.

### Fix profile

**Purpose:** Use a cloned sandbox to produce a tested `FixOutcome`.

| Field | Value |
|---|---|
| `feature` | `Feature.FIX` |
| `name` | `fix` |
| `sandbox_requirement` | `REQUIRED` |
| `checkpoint_enabled` | `True` |
| `response_schema` | `FixOutcomeSchema` |
| tools | `read_file`, `write_file`, `list_files`, `run_command`, `git_diff`, `search_files` from `_fix_tools.py` |
| parser | existing `parse_structured_response(...) -> FixOutcome` |

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
| `name` | `chat` |
| `sandbox_requirement` | `FORBIDDEN` initially |
| `checkpoint_enabled` | `False` initially |
| `response_schema` | `None` |
| tools | none initially |
| parser | extract final assistant text |

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
first migration:

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

### Cancellation

The runtime should expose a middleware hook for cancellation, but business-level
checkpoints still belong around slow workflow operations:

1. Before and after agent invocation.
2. After sandbox clone.
3. Before branch creation.
4. Before push.
5. Before PR open.

This keeps cancellation responsive even when the agent itself is not currently
inside a LangGraph node that checks cancellation.

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

### Initial standard middleware

The runtime should centralize the middleware stack, but rollout can start with
the behavior already proven in evals:

1. Model call limit.
2. Tool call limit.
3. Convergence/repetition guard.
4. Structured finalizer when a schema is present.
5. Trace/metadata enrichment.

Budget and cancellation middleware can land behind the same runtime seam once
their production ports are ready. The interface should be designed now so adding
them does not change profile callers.

### Ordering rule

Middleware order is runtime-owned and tested. Profiles may supply extra
middleware only through an explicit `extra_middleware` field, appended after
standard safety middleware. Profiles must not replace the standard stack.

---

## Observability

Every runtime invocation should attach stable metadata:

| Field | Source |
|---|---|
| `feature` | `profile.feature.value` |
| `agent_profile` | `profile.name` |
| `run_id` | `request.run_id` |
| `task_id` | `request.event.delivery_id` or dispatch metadata when supplied |
| `repo` | `request.event.repo` |
| `actor` | `request.event.actor` |
| `model` | normalized display model |
| `checkpoint_enabled` | runtime decision |
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

The runtime must log full exception context, but workflow comments must stay
stable and non-sensitive.

---

## Testing strategy

### Runtime unit tests

Add tests under `tests/infrastructure/agents/test_runtime.py`:

1. Builds `create_deep_agent` with normalized model name.
2. Passes `response_format` only when profile defines a schema.
3. Adds `configurable.thread_id` only when run id and checkpointer are both present.
4. Does not add checkpoint config when either part is missing.
5. Enforces `REQUIRED`, `OPTIONAL`, and `FORBIDDEN` sandbox requirements.
6. Calls `profile.parse_result(...)` and returns the domain object.
7. Wraps DeepAgents exceptions in stable runtime errors.

### Profile migration tests

Keep existing responder tests and update expectations to assert delegation:

1. Review profile builds read-only tools and returns `ReviewFindings`.
2. Fix profile requires sandbox and returns `FixOutcome`.
3. Chat profile extracts text and registers no tools.
4. No test makes a real network or model call; `create_deep_agent` stays monkeypatched.

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

1. Add `model_names.py`.
2. Add `profiles.py`.
3. Add `runtime.py`.
4. Add runtime unit tests with fake profiles and monkeypatched `create_deep_agent`.

No workflow behavior changes in this phase.

### Phase 2: Review migration

1. Convert review prompt/tool/schema/parser into `ReviewProfile`.
2. Keep `DeepAgentsReviewResponder.review_for_event(...)` as compatibility seam.
3. Update review use case to pass `run_id` and `agent_checkpointer`.
4. Run review responder and review use-case tests.

### Phase 3: Fix migration

1. Convert fix prompt/tool/schema/parser into `FixProfile`.
2. Keep `DeepAgentsFixResponder.fix_for_event(...)` as compatibility seam.
3. Update fix use case to pass `run_id` and `agent_checkpointer`.
4. Confirm missing sandbox path still degrades before runtime invocation.

### Phase 4: Chat migration

1. Convert chat prompt/text parser into `ChatProfile`.
2. Remove stale "ACK only" wording from code comments and GitHub help copy if behavior is now real freeform reply.
3. Keep chat tool list empty until a separate read-only chat tools design lands.

### Phase 5: Cleanup and docs

1. Delete duplicated `_normalize_model_name` helpers.
2. Document the runtime/profile pattern in `openbot/infrastructure/agents/__init__.py` or a short `docs/architecture` note.
3. Update eval docs only if production/eval vocabulary changes. `deepagents_baseline` remains the eval comparator.

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

---

## Acceptance criteria

The implementation is complete when:

1. Review, fix, and chat all execute through `BaseDeepAgentRuntime`.
2. Use cases no longer call `create_deep_agent` indirectly through bespoke construction code; only runtime does.
3. Review/fix pass `run_id` and `agent_checkpointer` into the runtime path.
4. Review remains read-only and fix remains the only migrated profile with write/execute tools.
5. Existing domain return types are unchanged: `ReviewFindings`, `FixOutcome`, and `str` for chat.
6. Existing responder class names still import successfully.
7. Unit tests cover runtime checkpoint activation, sandbox requirement enforcement, and structured result parsing.
8. No production module imports from `evals.*`.

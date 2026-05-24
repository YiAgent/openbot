# Changelog

All notable changes to OpenBot are documented in this file.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

## [0.1.1] - 2026-05-24

### Added

- `openbot.evaluation` facade: `EvalChannelAdapter` (read-only GitHub port for eval harness),
  `run_review_sample`, `run_fix_sample`, `run_chat_sample` — single entry points that wire
  the full production workflow without side effects (no real PRs/comments created)
- `build_sandbox_factory` composition-root helper in `openbot.application.sandbox_factory_deps`;
  wired through the worker `consume_loop` → `execute_handler` path
- `evals/runtime/` package: merges former `evals/common/` + `evals/inspect/` into one canonical
  location; exposes `config`, `datasets`, `environment`, `hf_datasets`, `langsmith`,
  `prediction_export`, `predictions`
- Thin eval solvers: `evals.solvers.fix`, `evals.solvers.chat`, `evals.solvers.test_generation`
  delegate directly to `openbot.evaluation` — no DeepAgents runtime dependency

### Changed

- Evals sandbox lifecycle decoupled from eval config: `SandboxSettings` and all
  `OPENBOT_SANDBOX_BACKEND` / `OPENBOT_DAYTONA_*` / `OPENBOT_MODAL_*` env-var constants
  removed from `evals/runtime/config.py` (sandbox is an OpenBot product concern, not eval-owned)
- `solver_family_baseline` renamed from `"deepagents_baseline"` → `"openbot_agent"` across
  all tasks, LangSmith experiment entries, and test fixtures
- Review solver severity mapping extended: domain `"critical"` now maps to eval `"high"`
  (previously only `"nit"` → `"low"` was handled; silent pass-through could corrupt scorer rows)
- `fix.py` solver hoists `Settings()` + `build_sandbox_factory()` to solver scope (once per task
  instead of once per sample — avoids hundreds of redundant `.env` reads on large SWE-bench runs)

### Removed

- `evals/agents/` (7 files): `BaseDeepAgentRuntime`, `AgentProfile`, `deepagents_*` driver code
  — all solver logic now lives in `openbot.evaluation`
- `evals/sandboxes/` (6 files): Daytona / Docker / Modal backend implementations and factory —
  sandbox lifecycle now owned by `openbot.application.sandbox_factory_deps`
- 9 obsolete test files (`test_agents_layer`, `test_convergence_middleware`,
  `test_deepagents_budgets`, `test_deepagents_resilience`, `test_docker_backend`,
  `test_sandbox_factory`, `test_structured_finalizer`, `test_termination`, `test_review_solver`)

### Fixed

- Classifier: strip markdown code fences from LLM JSON response before parsing
  (GLM-5.1 wraps JSON in ```json``` blocks; caused silent triage failures)
- Classifier: use model router + GLM proxy `api_base` (hardcoded model was bypassing routing)
- Agent recursion limits: review 25 → 100, chat 10 → 30 to prevent `GraphRecursionError` on
  complex PRs and multi-turn conversations
- `ToolCallLimitMiddleware.exit_behavior` reverted to `'continue'` after `'end'` truncated
  agent responses mid-thought

## [0.1.0] - 2026-05-23

### Added

**Unified Sandbox Entry — single dispatch path for all agent workflows**
- `BaseDeepAgentRuntime`: unified async runtime with pluggable `AgentProfile` protocol, middleware
  stack (`ToolCallLimitMiddleware`, `ModelCallLimitMiddleware`), and per-request `RunnableConfig`
  (recursion limit, Langfuse callbacks, optional LangGraph checkpoint)
- `AgentProfile` protocol + `ReviewProfile`, `FixProfile`, `ChatProfile` implementations; all three
  responders (`deepagents_review`, `deepagents_fix`, `deepagents_chat`) now delegate to the runtime
- `model_names.py`: `normalize_for_langchain` (slash→colon normalisation) + `display_name` (strips
  provider prefix for trace metadata)
- `profiles.py`: typed `AgentRequest`, `AgentRunLimits`, `SandboxRequirement` dataclasses; structured
  error hierarchy (`AgentSandboxRequiredError`, `AgentBudgetExhaustedError`, `AgentTimeoutError`, …)

**Sandbox Snapshot Cache**
- `SandboxCachePort` protocol + three implementations: `DaytonaSnapshotCache` (production, LRU + 24h
  TTL, per-repo eviction), `InMemorySandboxCache` (fast test doubles), `NoOpSandboxCache`
- `SandboxedHandle` (immutable checkout + token triple passed to handlers)
- `_cache_key` (SHA-256–based, deterministic, installation-scoped)
- `SandboxPolicy` resolver (`derive_sandbox_policy`): OR-merge of static policy with live classifier
  output; handles triage, review, fix, chat bypass rules
- Warm-cache acquire + cold-path publish wired in `dispatcher._run_with_sandbox`; two-pass secret
  sweep before snapshot creation
- Demo 12 (e2e): chat cache-hit P95 < 1 s

**Langfuse Observability**
- `observability.py`: `init_langfuse()` / `get_langfuse_handler()` alongside existing LangSmith init
- Per-request `CallbackHandler` injected into `BaseDeepAgentRuntime.run()` so every agent trace lands
  in Langfuse without touching LangSmith wiring
- `LANGFUSE_BASE_URL` / `LANGFUSE_HOST` resolution aligned with SDK priority order

**Worker & Queue Simplification**
- v3-only `TaskSpec` pipeline; legacy v1/v2 `QueuePayload` paths removed
- `execute_handler` / `TaskSpec.from_event_and_dispatch` as single worker entry point
- Agent checkpoint recovery: `AsyncPostgresSaver` wired for LangGraph state persistence across
  webhook retries; `cancel_checkpoint` called on 14 cancellation paths

**Classifier-Routed Dispatcher**
- `_run_with_sandbox`: resolver → clone → handler inside `async with factory()` guaranteeing sandbox
  lifetime; degrades gracefully on clone / token / resolution failure
- `sandbox_cache_acquire_seconds`, `sandbox_cache_total`, `sandbox_cache_publish_total` Prometheus
  counters + bypass-source label for ops dashboards

### Changed

- `deepagents_review` / `deepagents_fix` / `deepagents_chat`: all three responders consolidated under
  `AgentProfile`; `ToolBudget` retired in favour of `ToolCallLimitMiddleware`
- Checkout resolver now calls `get_pull_request` + `get_default_branch_sha` for PR-open events;
  `get_default_branch_sha` raises on malformed GitHub API response (previously returned `""`)
- `ingest_webhook` enqueues `TaskSpec` v3; Redis is now a required dependency (no more
  `BackgroundTask` fallback); returns structured `IngestResult` across all paths

### Fixed

- `dispatcher.py`: `_safe_publish` now `await`ed inside the `async with` sandbox block instead of
  scheduled as a fire-and-forget task — prevents use-after-close where the sandbox was deleted before
  the publish ran, making the warm cache silently a no-op in production
- `cache_daytona.py`: apply `_redact_tokens` to git stderr before embedding in `CacheCorruptedError`
  — prevents GitHub installation tokens from leaking into exception text, log lines, and Sentry
- `github.py`: `get_default_branch_sha` raises `ValueError` on malformed API response instead of
  returning `""` which caused a downstream silent bad `git clone`
- `_fix_tools.py`: clamp LLM-supplied `timeout_seconds` to 300 s to prevent thread-pool stall
- `runtime.py`: optimistic write to `_REGISTERED_MODELS` before calling `register_harness_profile`
  to close concurrent double-registration window
- `AsyncPostgresSaver` postgres URL dialect normalisation (postgres→postgresql+asyncpg)
- Langfuse host resolution priority (`LANGFUSE_BASE_URL` before legacy `LANGFUSE_HOST`)
- `cache_fake.py`: `InMemorySandboxCache.acquire()` now **consumes** the entry (removes from `_index`
  under the lock) before releasing — prevents two concurrent callers from receiving the same live
  `SandboxPort` object; matches `DaytonaSnapshotCache`'s fresh-workspace-per-acquire semantics
- `cache_fake.py`: `_inject_token` now validates the HTTPS hostname against `_ALLOWED_CLONE_HOSTS`
  (`{"github.com"}`) before injecting an installation token — prevents token leakage to non-GitHub
  URLs; mirrors the allowlist guard in `DaytonaSandboxAdapter._inject_token`

### Removed

- `ToolBudget` (replaced by `ToolCallLimitMiddleware` in runtime stack)
- Worker v1/v2 `QueuePayload` / `run_dispatch` deserialization paths (v3-only from this release)
- Background queue variants (BackgroundTask v1/v2); worker now v3-only

[0.1.0]: https://github.com/YiAgent/openbot/releases/tag/v0.1.0

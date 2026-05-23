# OpenBot Evals Runtime Redesign — use production agents and harness

**Status:** design. Awaiting implementation plan.
**Date:** 2026-05-22
**Branch (proposed):** `refactor/evals-runtime-openbot-harness`
**Related specs:** `2026-05-22-v0-1-product-closure-design.md`, `2026-05-21-unified-sandbox-entry-design.md`, `2026-05-22-deepagents-runtime-design.md`.

---

## Goal

Redesign the eval system so it measures OpenBot as the product actually runs.

The four existing eval surfaces remain:

| Surface | Benchmark / dataset | Product capability under test |
|---|---|---|
| Review | Martian Code Review Bench mirror | OpenBot review workflow |
| Fix | SWE-bench Verified | OpenBot fix workflow / patch generation |
| Chat | SWE-QA-Pro | OpenBot chat / repo QA workflow |
| Test generation | SWT-Bench Verified | OpenBot test-generation capability once it exists |

The eval runner must no longer own a parallel agent stack. The current
`evals/agents/*` code was created to get benchmark runs working before the real
OpenBot agents existed. That layer is now misleading: it can pass or fail while
testing a different prompt, tool set, model resolver, sandbox, and loop behavior
than production OpenBot.

Target state:

1. `evals/` owns datasets, Inspect task definitions, scorers, LangSmith
   experiment projection, and prediction export.
2. `openbot/` owns agents, model routing, harness behavior, sandbox creation,
   repository checkout, task context construction, and workflow-specific
   execution.
3. Eval solvers call a stable `openbot.evaluation` facade. They do not import
   `openbot.infrastructure.agents.*`, do not create sandboxes, and do not clone
   repositories.
4. LangSmith uploads continue to work for traces, per-sample feedback, and
   SWE/SWT prediction export metadata.

---

## Non-goals

1. Do not change benchmark datasets, scorers, judge prompts, or official
   grading contracts in this refactor.
2. Do not tune OpenBot prompts just to improve eval numbers.
3. Do not add an eval-only sandbox backend.
4. Do not keep `deepagents_baseline` as a hidden compatibility layer.
5. Do not run GitHub side effects such as branch push or PR creation from
   offline patch-generation evals.

---

## Current problem

The current eval tree mixes three responsibilities:

1. Benchmark runner code.
2. OpenBot-like agent implementation.
3. Sandbox / repository setup.

The problematic areas are:

| Current area | Problem |
|---|---|
| `evals/agents/*` | Defines eval-only prompts, model resolver, middleware, structured finalizer, and DeepAgents factories. This is not the product agent. |
| `evals/sandboxes/*` | Owns Docker/Modal/Daytona sandbox and clone logic. This bypasses OpenBot's unified sandbox entry. |
| `evals/common/*` and `evals/inspect/*` | Artificial split; both are eval runtime support code. |
| `evals/solvers/swe_fix.py`, `swe_test.py`, `swe_qa.py` | Names mix benchmark, capability, and implementation detail. |
| `deepagents_baseline_*` task/solver identifiers | Encode an obsolete implementation strategy into public eval surfaces. |

The result is that a passing eval can still leave the production OpenBot path
broken, especially around `resolve_checkout`, `sandbox_factory`, `sandbox.clone`,
`SandboxedHandle`, real model routing, and responder wiring.

---

## Locked decisions

### 1. Evals do not own agents

Delete the eval-only agent layer:

```text
evals/agents/
```

Eval solvers must call `openbot.evaluation.*`, not `evals.agents.*`.

### 2. Evals do not own sandbox or repo checkout

Delete the eval sandbox layer:

```text
evals/sandboxes/
```

Sandbox selection, sandbox lifecycle, repository checkout, token handling, and
`SandboxedHandle` construction are OpenBot harness concerns. Offline evals must
exercise that product-owned path.

### 3. `common/` and `inspect/` merge into `runtime/`

The old split is not meaningful after agent/sandbox code is removed. Both
directories contain eval runner support code, so they become:

```text
evals/runtime/
```

### 4. Naming avoids implementation details

File names must describe stable responsibility, not current implementation.

Avoid:

```text
deepagents_baseline_*
*_openbot_prod
swe_fix.py
swe_test.py
swe_qa.py
task_runtime.py
docker_backend.py
daytona_backend.py
modal_backend.py
```

Use:

```text
tasks/review_martian.py
tasks/fix_swe_bench.py
tasks/chat_swe_qa.py
tasks/test_swt_bench.py

solvers/review.py
solvers/fix.py
solvers/chat.py
solvers/test_generation.py

runtime/environment.py
runtime/langsmith.py
runtime/predictions.py
```

### 5. OpenBot exposes a stable evaluation facade

Add a product-owned facade:

```text
openbot/evaluation/
```

This is the only OpenBot import surface used by `evals/solvers/*`.

---

## Target architecture

```text
Inspect Task
  -> evals.solvers.<capability>
  -> openbot.evaluation.run_<capability>_sample(...)
  -> OpenBot-owned harness / dispatcher / responder path
  -> evals scorer
  -> LangSmith experiment + feedback
```

`evals` remains the external measurement bench. `openbot` is the system under
test.

### Boundary rules

| Layer | Owns | Must not own |
|---|---|---|
| `evals/tasks` | Inspect `Task` definitions and dataset selection. | Agent prompts, sandbox setup, repo checkout. |
| `evals/solvers` | Thin Inspect `Solver` adapters. | OpenBot agent internals or product workflow decisions. |
| `evals/runtime` | Eval config, dataset loaders, LangSmith projection, prediction export. | Model routing, DeepAgents middleware, sandbox backend selection. |
| `evals/scorers` | Benchmark scoring and judge calls. | Agent execution. |
| `openbot/evaluation` | Stable facade for running OpenBot against benchmark samples. | Inspect-specific scoring. |
| `openbot/application` / `openbot/infrastructure` | Real workflow execution, sandbox, checkout, agents, model routing. | Benchmark-specific score math. |

---

## OpenBot evaluation facade

Add:

```text
openbot/evaluation/
├── __init__.py
├── samples.py
├── results.py
├── runner.py
└── adapters.py
```

### `samples.py`

Defines benchmark-neutral input objects:

```text
ReviewSample
FixSample
ChatSample
TestGenerationSample
```

These are not Inspect types. They are OpenBot-owned request objects with the
minimum fields required to build a realistic `UnifiedEvent` and let OpenBot
resolve checkout using its normal rules.

### `results.py`

Defines product-facing eval outputs:

```text
ReviewEvalResult
PatchEvalResult
ChatEvalResult
UnsupportedCapabilityResult
```

These results contain only data needed by eval scorers/exporters:

| Result | Required fields |
|---|---|
| `ReviewEvalResult` | `summary`, `findings`, optional raw metadata. |
| `PatchEvalResult` | `model_patch`, `agent_summary`, `tests_passed`, optional tool/test metadata. |
| `ChatEvalResult` | `answer`, optional citations/evidence metadata. |
| `UnsupportedCapabilityResult` | `reason`, `capability`, `sample_id`. |

### `runner.py`

Exposes:

```text
async def run_review_sample(sample: ReviewSample) -> ReviewEvalResult
async def run_fix_sample(sample: FixSample) -> PatchEvalResult
async def run_chat_sample(sample: ChatSample) -> ChatEvalResult
async def run_test_generation_sample(sample: TestGenerationSample) -> PatchEvalResult | UnsupportedCapabilityResult
```

The runner is product code. It may call internal OpenBot functions, including
dispatcher/harness helpers, but that coupling stays inside `openbot/`.

### `adapters.py`

Contains OpenBot-owned adapters that convert benchmark samples into product
execution surfaces:

1. `UnifiedEvent` builders.
2. Channel adapter shims needed by OpenBot workflows.
3. Benchmark-safe GitHub side-effect handling.

This code lives in `openbot/`, not `evals/`, because it is part of the supported
way to run OpenBot against offline benchmark samples.

---

## Harness requirement

OpenBot already has the correct production direction:

```text
resolve_checkout
  -> sandbox_factory()
  -> sandbox.clone(...)
  -> SandboxedHandle
  -> workflow handler / responder
```

The eval redesign must exercise this path. Therefore the implementation plan
must first complete product-side sandbox factory wiring:

1. Add `build_sandbox_factory(settings)` in `openbot/core/dependencies.py`.
2. Pass that factory into worker `consume_loop(...)`.
3. Pass it through `consume_loop(...)` into `_execute_task_spec(...)`.
4. Pass it into `execute_handler(...)`.
5. Ensure `execute_handler(...)` is the single production path that opens and
   clones a sandbox for sandbox-required workflows.

No eval code may compensate for missing product-side sandbox wiring.

---

## Workflow-specific behavior

### Review

`evals/tasks/review_martian.py` becomes `evals/tasks/review_martian.py`
or keeps its name if minimizing churn. It calls:

```text
evals.solvers.review.review_solver()
  -> openbot.evaluation.run_review_sample(...)
```

The OpenBot result is converted to the Martian scorer shape:

```text
{"file": str, "line": int | None, "body": str, "severity": "low" | "medium" | "high"}
```

Severity mapping from OpenBot's richer set:

| OpenBot severity | Eval severity |
|---|---|
| `critical` | `high` |
| `high` | `high` |
| `medium` | `medium` |
| `low` | `low` |
| `nit` | `low` |

### Fix

`evals/tasks/fix_swe_bench.py` calls:

```text
evals.solvers.fix.fix_solver()
  -> openbot.evaluation.run_fix_sample(...)
```

The OpenBot runner must use product-owned sandbox/checkout setup, run the real
fix responder, and return a patch. It must not push branches or open GitHub PRs
in offline eval mode.

The solver writes `SweBenchPrediction` through the existing prediction exporter.
Official grading remains outside Inspect through the SWE-bench harness.

### Chat

`evals/tasks/chat_swe_qa.py` calls:

```text
evals.solvers.chat.chat_solver()
  -> openbot.evaluation.run_chat_sample(...)
```

The eval must expose the current product capability honestly. If production
OpenBot chat has no repo-grounded tools, the eval should score that behavior as
is. Do not add eval-only read/search tools.

### Test generation

`evals/tasks/test_swt_bench.py` remains as an eval surface, but the solver must
not keep the old `evals.agents.test_generation` implementation.

Until OpenBot product code has a real test-generation capability, the OpenBot
facade returns:

```text
UnsupportedCapabilityResult(capability="test_generation", reason="not_implemented")
```

The task emits an empty validated prediction with metadata
`unsupported=true`. This keeps Inspect and LangSmith experiment rows stable
while making it impossible to confuse the result with a product attempt.

---

## Final directory structure

```text
evals/
├── Makefile
├── README.md
├── runtime/
│   ├── __init__.py
│   ├── config.py
│   ├── datasets.py
│   ├── environment.py
│   ├── hf_datasets.py
│   ├── langsmith.py
│   ├── prediction_export.py
│   └── predictions.py
├── scorers/
│   ├── __init__.py
│   ├── review_judge.py
│   ├── review_overlap.py
│   ├── swe_qa_judge.py
│   └── swe_qa_score.py
├── solvers/
│   ├── __init__.py
│   ├── chat.py
│   ├── fix.py
│   ├── review.py
│   └── test_generation.py
├── tasks/
│   ├── __init__.py
│   ├── chat_swe_qa.py
│   ├── fix_swe_bench.py
│   ├── review_martian.py
│   └── test_swt_bench.py
├── scripts/
│   ├── __init__.py
│   ├── build_chat_swe_qa_dataset.py
│   ├── build_fix_swe_bench_dataset.py
│   ├── build_review_martian_dataset.py
│   ├── build_test_swt_bench_dataset.py
│   ├── writeback_swe_bench_grades.py
│   └── writeback_swt_bench_grades.py
└── third_party/
    └── swt_bench/
```

```text
openbot/evaluation/
├── __init__.py
├── adapters.py
├── results.py
├── runner.py
└── samples.py
```

---

## Move / delete map

### Delete

```text
evals/agents/
evals/sandboxes/
```

### Merge

```text
evals/common/config.py              -> evals/runtime/config.py
evals/common/datasets.py            -> evals/runtime/datasets.py
evals/common/prediction_export.py   -> evals/runtime/prediction_export.py
evals/common/predictions.py         -> evals/runtime/predictions.py
evals/inspect/hf_datasets.py        -> evals/runtime/hf_datasets.py
evals/inspect/langsmith.py          -> evals/runtime/langsmith.py
evals/inspect/task_runtime.py       -> evals/runtime/environment.py
```

### Delete if no remaining import after solver rewrite

```text
evals/common/messages.py
evals/common/termination.py
evals/common/usage.py
```

### Rename tasks

```text
evals/tasks/fix_swe_bench_verified.py   -> evals/tasks/fix_swe_bench.py
evals/tasks/chat_swe_qa_pro.py          -> evals/tasks/chat_swe_qa.py
evals/tasks/test_swt_bench_verified.py  -> evals/tasks/test_swt_bench.py
evals/tasks/review_martian.py           -> evals/tasks/review_martian.py
```

### Rename solvers

```text
evals/solvers/review.py    -> evals/solvers/review.py
evals/solvers/swe_fix.py   -> evals/solvers/fix.py
evals/solvers/swe_qa.py    -> evals/solvers/chat.py
evals/solvers/swe_test.py  -> evals/solvers/test_generation.py
```

All renamed solver files must be rewritten as thin OpenBot facade adapters, not
mechanically moved with their old implementation.

---

## Runtime config after cleanup

`evals/runtime/config.py` keeps only eval-owned settings:

1. dataset names and sources,
2. judge model configuration,
3. LangSmith project/feedback settings,
4. prediction output directory.

Remove eval-owned agent/sandbox settings:

```text
OPENBOT_DEEPAGENTS_MODEL
OPENBOT_DEEPAGENTS_MODEL_CALL_LIMIT
OPENBOT_DEEPAGENTS_TOOL_CALL_LIMIT
OPENBOT_DEEPAGENTS_RECURSION_LIMIT
OPENBOT_DEEPAGENTS_MODEL_TIMEOUT_S
OPENBOT_DEEPAGENTS_MODEL_MAX_RETRIES
OPENBOT_DEEPAGENTS_MAX_OUTPUT_TOKENS
OPENBOT_DEEPAGENTS_THINKING_LEVEL
OPENBOT_SANDBOX_BACKEND
OPENBOT_DAYTONA_IMAGE
OPENBOT_DAYTONA_AUTO_STOP_MIN
OPENBOT_DAYTONA_AUTO_ARCHIVE_MIN
OPENBOT_DAYTONA_AUTO_DELETE_MIN
OPENBOT_MODAL_IMAGE
OPENBOT_MODAL_APP
```

OpenBot product settings own model and sandbox behavior.

---

## LangSmith behavior

LangSmith remains required for:

1. review and chat dataset loading when those datasets are mirrored there,
2. per-sample experiment rows,
3. per-sample feedback values,
4. trace routing,
5. SWE/SWT prediction export metadata and writeback scripts.

Naming changes:

| Old | New |
|---|---|
| `deepagents_baseline` | `openbot_agent` |
| `deepagents_baseline_review_solver` | `review_solver` |
| `deepagents_baseline_swe_solver` | `fix_solver` |
| `deepagents_baseline_swe_qa_solver` | `chat_solver` |
| `deepagents_baseline_swt_solver` | `test_generation_solver` |

Every experiment row must include:

```text
solver_family = "openbot_agent"
solver_id = "review" | "fix" | "chat" | "test_generation"
openbot_git_sha = <current repo sha>
dataset_version = <catalog dataset version>
capability = <same as solver_id>
```

For unsupported test generation, LangSmith metadata must make the skip explicit:

```text
unsupported = true
unsupported_reason = "not_implemented"
```

---

## Test updates

Delete tests that validate the removed eval-only agent/sandbox layer:

```text
tests/eval/test_agents_layer.py
tests/eval/test_deepagents_budgets.py
tests/eval/test_deepagents_resilience.py
tests/eval/test_convergence_middleware.py
tests/eval/test_structured_finalizer.py
tests/eval/test_docker_backend.py
tests/eval/test_sandbox_factory.py
```

Add or rewrite tests around:

```text
tests/eval/test_runtime_config.py
tests/eval/test_runtime_langsmith.py
tests/eval/test_task_wiring.py
tests/eval/test_solver_adapters.py
tests/evaluation/test_runner_review.py
tests/evaluation/test_runner_fix.py
tests/evaluation/test_runner_chat.py
tests/evaluation/test_runner_test_generation.py
```

Test intent:

1. `evals/solvers/*` imports only `openbot.evaluation`, `evals.runtime`, and
   Inspect types.
2. No file under `evals/` imports `deepagents` or `openbot.infrastructure.agents`.
3. No file under `evals/` creates a sandbox or clones a repo.
4. LangSmith wrapper still posts feedback and experiment rows.
5. OpenBot evaluation runner uses product-owned sandbox provisioning.

---

## Implementation order

1. Add product-side `build_sandbox_factory(...)` and wire it through the worker.
2. Add `openbot/evaluation` sample/result/runner facade.
3. Merge `evals/common` and `evals/inspect` into `evals/runtime` with import-only
   changes.
4. Rename task and solver files to the stable naming scheme.
5. Rewrite solvers to call `openbot.evaluation`.
6. Delete `evals/agents` and `evals/sandboxes`.
7. Remove eval-owned model/sandbox config.
8. Update README, Makefile targets, and tests.
9. Run unit tests and one smoke sample per supported eval surface.

---

## Acceptance criteria

1. `rg "evals\\.agents|evals\\.sandboxes|deepagents_baseline|OPENBOT_DEEPAGENTS|OPENBOT_SANDBOX_BACKEND" evals tests/eval`
   has no live hits except migration notes in docs.
2. `evals/` contains no sandbox backend, no repo clone implementation, and no
   OpenBot agent prompt/factory.
3. `evals/tasks/*` still expose the same four eval surfaces.
4. Review, fix, and chat eval solvers call `openbot.evaluation`.
5. Test-generation eval has an explicit unsupported behavior until the product
   capability exists.
6. LangSmith experiment upload still records per-sample feedback for review and
   chat, and export status for fix/test.
7. SWE-bench and SWT-Bench prediction JSONL schemas still validate.
8. `uv run pytest tests/eval tests/evaluation` passes.
9. A one-sample smoke run completes for review, fix, and chat when credentials
   are configured.

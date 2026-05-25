# Eval Data Unification — Design Spec

**Date:** 2026-05-24 (revised)
**Branch:** `refactor/evals-runtime-openbot-harness`
**Status:** approved

---

## 1. Problem

Eval data lifecycle is split across four near-duplicate `evals/scripts/build_*_dataset.py` (≈850 LOC), two near-duplicate `evals/scripts/writeback_*_grades.py` (≈510 LOC), and three `evals/runtime/{datasets,predictions,langsmith}.py` modules (≈1140 LOC). Each suite re-implements the same patterns — LangSmith publish, grade writeback, Example→Sample conversion, argparse boilerplate.

Additionally, the current (draft) design conflates two concerns inside `EvalDataset.build_task()`:

- **Task assembly** — pure, inspect-native: dataset + solver + scorer + metadata dict.
- **LangSmith experiment lifecycle** — side-effectful: `experiment.start()`, `experiment.wrap(scorer)`, `configure_tracing_for_dataset()`.

The LangSmith lifecycle already lives in `evals/runtime/langsmith_hook.py` (a global `@hooks` class that runs on every `inspect eval` invocation). Putting it *also* inside `build_task()` creates double-writes, tight coupling, and prevents the task files from being purely declarative.

---

## 2. Goal

One `evals/data/` package where each suite is a single `EvalDataset` instance owning:
`collect → publish → load → writeback grades`

And `EvalDataset.build_task()` produces a **pure `inspect_ai.Task`** with no LangSmith imports —
all observability goes through the existing hook.

Targets:
- ≈3000 LOC → ≈1200 LOC.
- Each `evals/tasks/*.py` shrinks to ~10 lines: `@task def f() -> Task: return SUITE.build_task(solver=...)`.
- `_experiment.py` shrinks to three utility functions (`git_sha`, `resolve_model_label`, `ensure_feedback_config`).
- One CLI: `python -m evals.data <verb> <suite>`.

---

## 3. Non-goals

- No change to scorer logic (`evals/scorers/*` untouched).
- No change to solver logic (`evals/solvers/*` untouched).
- No change to vendored harness (`evals/third_party/swt_bench/*` untouched).
- No change to LangSmith feedback key semantics.
- No change to LangSmith dataset names already published.
- No replacement of the offline-grading workflow (predictions JSONL + Docker harness).

---

## 4. Architecture

### 4.1 Package layout

```
evals/data/
├── __init__.py        — exports REVIEW, CHAT, FIX, SWT singletons + EvalDataset class
├── __main__.py        — CLI router: python -m evals.data <verb> <suite>
├── _base.py           — EvalDataset ABC + CollectedExample + PublishResult + WritebackSummary
├── _predictions.py    — Pydantic schemas + _AppendWriter + prediction_exporter scorer
│                        (moved verbatim from runtime/predictions.py)
├── _samples.py        — LangSmith → Inspect Sample converters + load helpers
│                        (moved verbatim from runtime/datasets.py)
├── _utils.py          — git_sha, resolve_model_label, ensure_feedback_config
│                        (subset of runtime/langsmith.py — experiment lifecycle removed)
├── _writeback.py      — generic run_writeback() driver (dedupes writeback_swe/swt_grades.py)
├── review.py          — ReviewDataset
├── chat.py            — ChatDataset
├── fix.py             — FixDataset (SweBenchPrediction, swe_bench_pass_at_1 writeback)
└── swt.py             — SwtDataset (SwtBenchPrediction, swt_bench_pass_at_1 writeback)
```

**Deleted:**
- `evals/scripts/build_*_dataset.py` × 4
- `evals/scripts/writeback_*_grades.py` × 2
- `evals/runtime/datasets.py`, `runtime/predictions.py`, `runtime/langsmith.py`

**Kept in place:**
- `evals/runtime/config.py` — process-wide config; used by scorers and hook.
- `evals/runtime/langsmith_hook.py` — the `@hooks` class that owns **all** LangSmith experiment lifecycle.

---

### 4.2 EvalDataset ABC (pure data lifecycle only)

```python
# evals/data/_base.py

class CollectedExample(TypedDict):
    inputs: dict[str, Any]
    outputs: dict[str, Any] | None
    metadata: dict[str, Any]

class EvalDataset(ABC):
    suite: ClassVar[str]             # "review" | "chat" | "fix" | "swt"
    dataset_version: str             # LangSmith dataset name (stable)
    instance_id_field: str = "id"   # inputs key for LangSmith ↔ Inspect id
    description: str = ""

    # ── upstream pull ──────────────────────────────────────────────────
    @abstractmethod
    def collect(self) -> list[CollectedExample]: ...
    # Pull from upstream (HF / GitHub PRs). Pure — no LangSmith side effects.

    @abstractmethod
    def example_to_sample(self, example: Any) -> inspect_ai.Sample: ...
    # Translate one LangSmith Example back to an Inspect Sample.

    # ── LangSmith publish ──────────────────────────────────────────────
    def publish(
        self,
        examples: list[CollectedExample],
        *,
        client: Any | None = None,
        chunk_size: int = 100,
    ) -> PublishResult: ...
    # list_datasets → create/replace dataset → chunked create_examples.

    # ── Inspect dataset load ───────────────────────────────────────────
    def load_for_inspect(self) -> MemoryDataset: ...
    # Calls _samples.langsmith_dataset(self.dataset_version, converter=self.example_to_sample).
    # Raises RuntimeError with actionable hint if the dataset is not published yet.

    # ── offline grade writeback (fix / swt only) ──────────────────────
    def writeback_grades(
        self,
        *,
        report_path: str,
        experiment_name: str,
        dry_run: bool = False,
        client: Any | None = None,
    ) -> WritebackSummary: ...
    # Default: raises NotImplementedError (review/chat have online grading).
    # FixDataset / SwtDataset override by calling _writeback.run_writeback(self, ...).

    # ── Inspect Task assembly (pure) ───────────────────────────────────
    @abstractmethod
    def build_task(
        self,
        *,
        solver: Any,
        scorer: Scorer | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Task: ...
    # Assembles and returns an inspect_ai.Task.
    # MUST NOT import from langsmith, _experiment, or langsmith_hook.
    # All LangSmith observability is handled by the hook (§4.3).
```

**Key constraint on `build_task()`:** it is a pure factory. It calls `self.load_for_inspect()`, picks a scorer, and returns `Task(dataset, solver, scorer, metadata)`. The `metadata` dict is the hook's configuration surface (§4.3).

---

### 4.3 Hook protocol — the metadata contract

`evals/runtime/langsmith_hook.py` (`_LangSmithExperimentHook`) is the single owner of the LangSmith experiment lifecycle. It runs automatically on every `inspect eval` invocation because its `@hooks` registration fires on package import (already wired in `evals/runtime/__init__.py`).

The hook reads the following keys from `Task.metadata` set by `build_task()`:

| metadata key | type | purpose |
|---|---|---|
| `dataset_version` | `str` | LangSmith dataset name — used to look up Example ids for Run anchoring |
| `solver_family` | `str` | Written to the experiment project metadata |
| `model` | `str` | Written to the experiment project metadata |
| `git_sha` | `str` | Written to the experiment project metadata |
| `instance_id_field` | `str` (default `"id"`) | Which `Example.inputs` key maps to the Inspect `sample_id` |
| `langsmith_experiment` | `bool` (default `True`) | Set to `False` to opt a task out of experiment anchoring |

The hook lifecycle:

```
on_task_start  → read metadata → provision LangSmith Experiment project + build Example id index
on_sample_end  → create Run anchored to dataset Example → create Feedback for every scorer score
on_task_end    → write aggregate metrics from data.log.results → drop session
```

No task or suite file imports from `langsmith` or `langsmith_hook`. The contract is the metadata dict.

---

### 4.4 `_utils.py` — three pure helpers

```python
# evals/data/_utils.py
# The ONLY survivors from evals/runtime/langsmith.py.

def git_sha() -> str: ...                  # subprocess git rev-parse HEAD
def resolve_model_label() -> str: ...      # reads Settings().model, fallback "openbot"
def ensure_feedback_config(client, key, config) -> None: ...  # idempotent LS config
def reset_feedback_cache_for_tests() -> None: ...              # test fixture
```

`LangSmithExperiment`, `configure_tracing_for_dataset`, `build_export_experiment` — **deleted**. These responsibilities now belong entirely to the hook.

---

### 4.5 Suite `build_task()` — what each one returns

#### fix / swt (prediction-exporter scorer, no LangSmith imports)

```python
# evals/data/fix.py
def build_task(self, *, solver, scorer=None, extra_metadata=None) -> Task:
    from evals.data._predictions import prediction_exporter, SweBenchPrediction
    from evals.data._utils import git_sha, resolve_model_label
    from evals.runtime.config import get_eval_config

    catalog = get_eval_config().catalog
    return Task(
        dataset=self.load_for_inspect(),
        solver=solver,
        scorer=prediction_exporter(
            dataset_version=self.dataset_version,
            schema=SweBenchPrediction,
            scorer_name=catalog.swe_export_feedback_key,
        ),
        metadata={
            "dataset_version": self.dataset_version,
            "solver_family": catalog.solver_family_baseline,
            "model": resolve_model_label(),
            "git_sha": git_sha(),
            "instance_id_field": self.instance_id_field,
            **(extra_metadata or {}),
        },
    )
```

#### review / chat (caller supplies scorer; task file imports scorer directly)

```python
# evals/data/review.py
def build_task(self, *, solver, scorer, extra_metadata=None) -> Task:
    # scorer is required and supplied by the task file
    from evals.data._utils import git_sha, resolve_model_label
    from evals.runtime.config import get_eval_config

    catalog = get_eval_config().catalog
    return Task(
        dataset=self.load_for_inspect(),
        solver=solver,
        scorer=scorer,
        metadata={
            "dataset_version": self.dataset_version,
            "solver_family": catalog.solver_family_baseline,
            "model": resolve_model_label(),
            "git_sha": git_sha(),
            "instance_id_field": self.instance_id_field,
            **(extra_metadata or {}),
        },
    )
```

---

### 4.6 Inspect Task files — final form

Each task file has **zero LangSmith imports**. The scorer is an inspect-native scorer. The hook handles observability.

```python
# evals/tasks/fix_swe_bench.py
from inspect_ai import Task, task
from evals.data import FIX
from evals.solvers.fix import openbot_fix_solver

@task
def fix_swe_bench() -> Task:
    return FIX.build_task(solver=openbot_fix_solver())
```

```python
# evals/tasks/review_martian.py
from inspect_ai import Task, task
from evals.data import REVIEW
from evals.scorers.review_judge import MARTIAN_JUDGE_MODEL_ID, MARTIAN_JUDGE_VERSION
from evals.solvers.review import openbot_review_solver
from evals.tasks._review_overlap_scorer import make_review_overlap_scorer

@task
def review_martian_openbot() -> Task:
    return REVIEW.build_task(
        solver=openbot_review_solver(),
        scorer=make_review_overlap_scorer(),
        extra_metadata={
            "judge_label": "martian_crb_verbatim",
            "judge_model_id": MARTIAN_JUDGE_MODEL_ID,
            "judge_prompt_version": MARTIAN_JUDGE_VERSION,
        },
    )
```

---

### 4.7 Hook extension: `on_task_end` aggregate metrics

Extend `_LangSmithExperimentHook.on_task_end` to push task-level aggregate metrics when `data.log.results` is available. This replaces the need to compute aggregates in the task or scorer:

```python
async def on_task_end(self, data: TaskEnd) -> None:
    session = self._sessions.pop(data.eval_id, None)
    if session and session.usable and data.log.results:
        from langsmith import Client
        client = Client()
        for scorer_name, scorer_metrics in (data.log.results.scores or {}).items():
            for metric_name, metric_val in (scorer_metrics.metrics or {}).items():
                if metric_val.value is None:
                    continue
                try:
                    client.create_feedback(
                        run_id=session.project_id,
                        key=f"{scorer_name}__{metric_name}",
                        score=float(metric_val.value),
                        comment=f"aggregate {metric_name} across {data.log.stats.completed_samples} samples",
                    )
                except Exception as exc:
                    logger.warning("hook: aggregate feedback failed: %s", exc)
```

---

### 4.8 CLI

`python -m evals.data <verb> <suite>`:

```
collect  <suite>              Pull upstream, emit CollectedExample JSON to stdout.
publish  <suite>              Read JSON on stdin, upsert into LangSmith dataset.
refresh  <suite>              collect → publish in one step (idempotent).
writeback <fix|swt>
    --report PATH             Path to Docker harness JSON report.
    --experiment NAME         LangSmith experiment project name.
    --dry-run                 Plan only, no create_feedback calls.
inspect  <suite>              Print dataset status (id, example count, sha256). Read-only.
```

---

### 4.9 Source policy

LangSmith is the single source of truth at Inspect runtime for every suite.

- `FixDataset.load_for_inspect()` / `SwtDataset.load_for_inspect()` — **changed**. Today these tasks load directly from HuggingFace. After the refactor they load from the LangSmith mirror (where grading-relevant fields `test_patch`, `FAIL_TO_PASS`, `PASS_TO_PASS`, `version`, `environment_setup_commit` live in `Example.metadata`). The mirror already publishes these (see `build_swe_bench_verified_dataset.py:74-87`).
- `load_for_inspect()` raises `RuntimeError` with a hint (`Run python -m evals.data refresh <suite>`) when the dataset is missing. No auto-publish.

---

## 5. Tests

```
tests/eval/test_data_unified.py
    — parametrized [REVIEW, CHAT, FIX, SWT] lifecycle tests
    — build_task returns a pure Task (asserts NO LangSmith import in task metadata)
    — publish chunks at chunk_size
    — load_for_inspect returns sorted MemoryDataset
    — load_for_inspect raises on missing dataset

tests/eval/test_data_predictions.py
    — schema validation
    — exporter appends JSONL
    — exporter skips empty patch
    — thread-safe concurrent append
    — public API parity (both old and new module paths)

tests/eval/test_data_writeback.py
    — parametrized [FIX, SWT]
    — creates one feedback per resolved instance
    — dry_run writes nothing
    — missing runs reported in summary
    — REVIEW/CHAT raise NotImplementedError

tests/eval/test_data_cli.py
    — refresh invokes collect + publish
    — unknown suite exits non-zero
    — writeback routes to suite.writeback_grades

tests/eval/test_langsmith_hook.py (extended from existing test_langsmith_experiments.py)
    — on_task_start provisions experiment from metadata["dataset_version"]
    — on_sample_end creates Run + Feedback
    — on_task_end writes aggregate metrics when data.log.results present
    — on_task_end is noop when results absent
    — hook disabled when LANGSMITH_API_KEY unset
    — metadata["langsmith_experiment"]=False opts out
```

**Deleted:** `test_datasets.py`, `test_predictions.py`, `test_inspect_helpers.py`, `test_langsmith_experiments.py`

**Kept:** `test_langsmith_routing.py`, `test_task_wiring.py`, `test_makefile.py`

---

## 6. Migration order (one green commit per step)

1. Create `evals/data/_predictions.py` — copy verbatim from `runtime/predictions.py`.
2. Create `evals/data/_samples.py` — copy verbatim from `runtime/datasets.py`.
3. Create `evals/data/_utils.py` — copy only `git_sha`, `resolve_model_label`, `ensure_feedback_config`, `reset_feedback_cache_for_tests` from `runtime/langsmith.py`.
4. Create `evals/data/_writeback.py` — generic writeback driver.
5. Create `evals/data/_base.py` — `EvalDataset` ABC + shared types + `sha256_examples` + `chunked_publish`.
6. Create `evals/data/review.py` — `ReviewDataset`.
7. Create `evals/data/chat.py` — `ChatDataset`.
8. Create `evals/data/fix.py` — `FixDataset` with pure `build_task()`.
9. Create `evals/data/swt.py` — `SwtDataset` with pure `build_task()`.
10. Create `evals/data/__main__.py` — CLI router.
11. Wire `evals/data/__init__.py` singletons + collapse `runtime/{datasets,predictions,langsmith}.py` to thin re-exports.
12. **Extend `langsmith_hook.py`** — add `on_task_end` aggregate metrics; assert that `build_task()` metadata keys are the only contract with the hook.
13. Rewrite `evals/tasks/*.py` — zero LangSmith imports, pure Task returns.
14. Rewrite `evals/Makefile` data/writeback targets to call new CLI.
15. Delete `evals/scripts/build_*` and `evals/scripts/writeback_*`.
16. Delete `evals/runtime/{datasets,predictions,langsmith}.py` shims.
17. Replace legacy test files with `test_data_*.py` suite.

---

## 7. Risks

- **Hook + scorer double-write eliminated**: current draft plan's `experiment.wrap(scorer)` caused LangSmith Feedback to be written both by the wrapped scorer AND the hook. This spec removes the wrapper — only the hook writes Feedback. Risk: if the hook is disabled (no `LANGSMITH_API_KEY`) there is no fallback. Mitigation: the hook logs a warning; offline runs silently skip LangSmith (intended behaviour).
- **HF metadata in mirror**: `test_patch`, `FAIL_TO_PASS`, `PASS_TO_PASS` must be present in LangSmith `Example.metadata` for `FixDataset.example_to_sample` to surface them. The existing `build_swe_bench_verified_dataset.py` already writes these fields — confirmed line 74-87. Mitigation: add an assertion in `test_data_unified.py::test_fix_example_to_sample_has_grading_fields`.
- **`configure_tracing_for_dataset` removal**: the old design called this to redirect LangChain auto-traces to the eval project. This responsibility moves to the hook's `on_task_start`. Verify the env-var redirect happens before any solver LLM call. Mitigation: `test_langsmith_routing.py` already covers this.

---

## 8. Out of scope

- Cross-suite dashboard.
- Auto-publish on Inspect run.
- Online grading (replacing the offline Docker harness).
- Moving `evals/runtime/config.py` — it serves scorers and the hook; not data-only.
- Changing `langsmith_hook.py` `on_sample_end` logic — it already works correctly.

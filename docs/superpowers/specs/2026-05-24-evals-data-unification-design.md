# Eval Data Unification — Design Spec

**Date:** 2026-05-24
**Branch:** `refactor/evals-runtime-openbot-harness`
**Author:** OpenBot eval refactor
**Status:** approved (pending user review)

## 1. Problem

Eval data lifecycle is split across four near-duplicate `evals/scripts/build_*_dataset.py` (≈850 LOC), two near-duplicate `evals/scripts/writeback_*_grades.py` (≈510 LOC), and three `evals/runtime/{datasets,predictions,langsmith}.py` modules (≈1140 LOC). Each suite (review / chat / fix / swt) re-implements the same patterns:

- LangSmith publish: `list_datasets` → `delete if force` → `create_dataset` → chunked `create_examples`.
- Grade writeback: load harness JSON → classify resolved/unresolved/error → look up Runs by `instance_id` → `create_feedback` with a per-suite key.
- Example → Inspect Sample conversion (three near-identical converters in `runtime/datasets.py`).
- Argparse boilerplate (`--force`, `--dry-run`, `--experiment-name`).

The lifecycle is also fragmented: `runtime/datasets.py` loads, `runtime/predictions.py` exports, `runtime/langsmith.py` records experiments, `scripts/build_*` publishes, `scripts/writeback_*` grades. A single suite's data story spans 5 files.

## 2. Goal

One `evals/data/` package where each suite is a single `EvalDataset` instance owning its full data lifecycle: collect from upstream → publish to LangSmith → load for Inspect → record predictions → record experiment runs → write back offline grades.

Targets:
- ≈3000 LOC → ≈1200 LOC.
- Each `evals/tasks/*.py` shrinks to ~10 lines (just `SUITE.build_task(solver=...)`).
- One CLI: `python -m evals.data <verb> <suite>`.

## 3. Non-goals

- No change to scorer logic (`evals/scorers/*` stays untouched).
- No change to solver logic (`evals/solvers/*` stays untouched).
- No change to vendored harness (`evals/third_party/swt_bench/*` stays untouched).
- No change to feedback key semantics (`swe_export_ok`, `swe_bench_pass_at_1`, etc. stay verbatim).
- No change to LangSmith dataset names already published (`martian_2026w20`, `chat_swe_qa_pro_v1`, `fix_swe_bench_verified`, `test_swt_bench_verified`).

## 4. Architecture

### 4.1 Package layout

```
evals/data/
├── __init__.py        — exports REVIEW, CHAT, FIX, SWT singletons + EvalDataset class
├── __main__.py        — CLI entry: python -m evals.data <verb> <suite>
├── _base.py           — EvalDataset ABC + chunked publish + sha256 + sample helpers
├── _experiment.py     — LangSmithExperiment lifecycle (moved from runtime/langsmith.py)
├── _predictions.py    — Pydantic schemas + JSONL appender + exporter scorer (moved from runtime/predictions.py)
├── _writeback.py      — generic grade writeback driver (parameterized by feedback_key)
├── review.py          — ReviewDataset (Martian; no predictions, no writeback)
├── chat.py            — ChatDataset (SWE-QA-Pro; no predictions, no writeback)
├── fix.py             — FixDataset (SWE-bench Verified; SweBenchPrediction + swe_bench_pass_at_1 writeback)
└── swt.py             — SwtDataset (SWT-bench Verified; SwtBenchPrediction + swt_bench_pass_at_1 writeback)
```

Deletions:
- `evals/scripts/build_*_dataset.py` × 4 (~850 LOC).
- `evals/scripts/writeback_*_grades.py` × 2 (~510 LOC).
- `evals/runtime/datasets.py`, `runtime/predictions.py`, `runtime/langsmith.py` (functionality moved into `evals/data/`).

Kept in place:
- `evals/runtime/config.py` — process-wide config (`get_eval_config`, `JudgeSettings`, `LangSmithSettings`, `PredictionsSettings`, `CatalogSettings`). Used by scorers too; not data-only.
- `evals/scorers/*`, `evals/solvers/*`, `evals/tasks/*`, `evals/third_party/*`.

### 4.2 EvalDataset abstraction

```python
# evals/data/_base.py
from typing import ClassVar, Protocol, TypedDict

class CollectedExample(TypedDict):
    """Canonical pre-publish row staged in memory before chunked upload."""
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    metadata: dict[str, Any]

class EvalDataset(ABC):
    suite: ClassVar[str]              # "review" | "chat" | "fix" | "swt"
    dataset_version: str              # LangSmith dataset name
    hf_dataset: str | None            # upstream HF repo (None for review — assembled from GH PRs)
    instance_id_field: str            # Inspect sample_id ↔ LangSmith Example.inputs[<field>] key
    description: str                  # written into LangSmith dataset description on publish

    # Lifecycle ----------------------------------------------------------------
    @abstractmethod
    def collect(self) -> list[CollectedExample]: ...
        # Pull from upstream → list of canonical {inputs, outputs, metadata} dicts.
        # Subclass owns upstream details. Base class is upstream-agnostic.

    def sha256(self, examples: list[CollectedExample]) -> str:
        # Deterministic SHA-256 over canonical JSONL form.

    def publish(self, *, force: bool, client: langsmith.Client | None = None) -> PublishResult:
        # 1. list_datasets(name=self.dataset_version)
        # 2. abort if exists and not force; delete if exists and force.
        # 3. create_dataset with self.description (suite injects revision / sha256 details).
        # 4. chunked create_examples (chunk=100).
        # Return PublishResult(dataset_id, sample_count, sha256, hf_revision).

    def load_for_inspect(self, *, client: langsmith.Client | None = None) -> MemoryDataset:
        # Pull every Example from LangSmith, convert via self.example_to_sample, sort by id.
        # Raise RuntimeError with `python -m evals.data publish <suite>` hint when missing.

    @abstractmethod
    def example_to_sample(self, example: langsmith.Example) -> inspect.Sample: ...
        # Subclass-specific shape (review: diff/findings, chat: question/answer,
        # fix/swt: instance_id/problem_statement/repo).

    # Experiment lifecycle -----------------------------------------------------
    def build_experiment(self, *, model: str | None = None, git_sha: str | None = None) -> LangSmithExperiment:
        # Defer to _experiment.LangSmithExperiment.start() with this suite's
        # dataset_version + instance_id_field + solver_family_baseline.

    # Predictions exporter (fix / swt only override) ---------------------------
    export_schema: type[BaseModel] | None = None       # SweBenchPrediction / SwtBenchPrediction / None
    export_feedback_key: str | None = None             # "swe_export_ok" / "swt_export_ok" / None

    def build_export_scorer(self, experiment: LangSmithExperiment) -> Scorer:
        # Combines _predictions.prediction_exporter + experiment.wrap.
        # Used only by fix/swt. review/chat pass scorer=... to build_task() instead;
        # default implementation here raises NotImplementedError when export_schema is None.

    # Offline grading writeback (fix / swt only override) ---------------------
    harness_feedback_key: str | None = None            # "swe_bench_pass_at_1" / None

    def writeback_grades(
        self,
        *,
        report_path: Path,
        experiment_name: str,
        dry_run: bool = False,
        client: langsmith.Client | None = None,
    ) -> WritebackSummary:
        # Defer to _writeback.run with self.harness_feedback_key.
        # Raise NotImplementedError when harness_feedback_key is None
        # (review/chat have no offline harness — judge is online).

    # Inspect Task assembly ---------------------------------------------------
    def build_task(
        self,
        *,
        solver: Solver,
        scorer: Scorer | None = None,         # None → use build_export_scorer (fix/swt) or raise (review/chat)
        scorer_name: str | None = None,
        feedback_key: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Task:
        # 1. configure_tracing_for_dataset(self.dataset_version)
        # 2. experiment = self.build_experiment(...)
        # 3. dataset = self.load_for_inspect()
        # 4. wrapped_scorer = experiment.wrap(scorer, scorer_name=..., feedback_key=...)
        #    when scorer given, else self.build_export_scorer(experiment).
        # 5. assemble Inspect Task with metadata = experiment.metadata() | (extra_metadata or {}).
```

Suite singletons live in `__init__.py`:

```python
from evals.data.review import ReviewDataset
from evals.data.chat import ChatDataset
from evals.data.fix import FixDataset
from evals.data.swt import SwtDataset

REVIEW = ReviewDataset()
CHAT = ChatDataset()
FIX = FixDataset()
SWT = SwtDataset()

__all__ = ["EvalDataset", "REVIEW", "CHAT", "FIX", "SWT", "DATASETS"]

DATASETS: dict[str, EvalDataset] = {
    "review": REVIEW,
    "chat": CHAT,
    "fix": FIX,
    "swt": SWT,
}
```

### 4.3 CLI

`python -m evals.data <verb> <suite> [flags]`:

```
publish <review|chat|fix|swt|all> [--force] [--revision REV]
    Pull upstream, compute sha256, create / replace LangSmith dataset.
    Aborts if dataset exists unless --force. `all` runs every suite; failures
    are aggregated and reported at the end.

writeback <fix|swt> --report PATH [--experiment-name NAME] [--dry-run]
    Read harness JSON, attach harness_feedback_key feedback to matching Runs.
    --experiment-name defaults to filename stem heuristic (per-suite).
    --dry-run skips create_feedback and just reports the planned writes.

inspect <suite>
    Print LangSmith dataset status: id, example count, sha256, hf_revision,
    description. Read-only.

load <suite> [--limit N]
    Print first N Inspect Samples (post-conversion) for debugging.
    Read-only. Default limit 3.
```

`evals/Makefile` `data-*` and `writeback-*` targets are rewritten to call this CLI; the doppler / `cd ..` wrappers are unchanged.

### 4.4 Source policy

LangSmith is the single source of truth at Inspect runtime for **every** suite:

- `ReviewDataset.load_for_inspect()`, `ChatDataset.load_for_inspect()` — already LangSmith-only (unchanged).
- `FixDataset.load_for_inspect()`, `SwtDataset.load_for_inspect()` — **changed**. Today `evals/tasks/fix_swe_bench.py` calls `load_issue_dataset(dataset_name=catalog.fix.hf_dataset)` which loads from HF directly. After refactor it loads from the LangSmith mirror, where every grading-relevant field (`test_patch`, `FAIL_TO_PASS`, `PASS_TO_PASS`, `version`, `environment_setup_commit`) lives in `Example.metadata`. The mirror already publishes these (see `build_swe_bench_verified_dataset.py:74-87`).
- The vendored offline harness still reads from HF on its own — that's its own concern, untouched.

**Hard contract**: `load_for_inspect()` raises `RuntimeError` with an actionable hint (`Run python -m evals.data publish <suite>`) when the LangSmith dataset is missing. CI runs `python -m evals.data publish all` before the eval suite. No `--auto-publish` flag — implicit publish would race two concurrent developers and contradict the existing "abort if exists" contract.

### 4.5 Inspect Task glue

Each `evals/tasks/*.py` collapses to:

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
# evals/tasks/review_martian.py — only this file keeps custom scorer plumbing
from inspect_ai import Task, task
from inspect_ai.scorer import mean, stderr
from evals.data import REVIEW
from evals.solvers.review import openbot_review_solver
from evals.scorers.review_judge import judge_verdict, MARTIAN_JUDGE_MODEL_ID, MARTIAN_JUDGE_VERSION
from evals.tasks._review_overlap_scorer import build_overlap_scorer  # extracted helper

@task
def review_martian_baseline_crb() -> Task:
    return REVIEW.build_task(
        solver=openbot_review_solver(),
        scorer=build_overlap_scorer(judge_verdict),
        scorer_name="review_overlap_f1",
        feedback_key="review_overlap_f1",
        extra_metadata={
            "judge_label": "martian_crb_verbatim",
            "judge_model_id": MARTIAN_JUDGE_MODEL_ID,
            "judge_prompt_version": MARTIAN_JUDGE_VERSION,
        },
    )
```

The review-overlap scorer body stays per-task (it's not data lifecycle). Same for the SWE-QA-Pro judge scorer. Only the `LangSmithExperiment.wrap`, `configure_tracing_for_dataset`, dataset loading, and experiment metadata stitching move into `EvalDataset.build_task`.

## 5. Tests

Replace the seven existing eval-data tests with three parametrized files:

```
tests/eval/test_data_unified.py
    @pytest.fixture(params=[REVIEW, CHAT, FIX, SWT])
    def suite(request) -> EvalDataset

    test_publish_aborts_when_exists_without_force(suite, mock_ls_client)
    test_publish_replaces_when_force(suite, mock_ls_client)
    test_publish_chunks_examples_at_100(suite, mock_ls_client)
    test_load_for_inspect_returns_sorted_samples(suite, mock_ls_client)
    test_load_for_inspect_raises_on_missing(suite, mock_ls_client)
    test_example_to_sample_roundtrip(suite)
    test_build_experiment_metadata_keys(suite)
    test_build_task_returns_inspect_task(suite)

tests/eval/test_data_predictions.py
    test_exporter_validates_schema()
    test_exporter_appends_jsonl()
    test_exporter_skips_empty_patch()
    test_exporter_thread_safe_concurrent_append()

tests/eval/test_data_writeback.py
    @pytest.fixture(params=[FIX, SWT])
    def graded_suite(request) -> EvalDataset

    test_classify_resolved_unresolved_error(graded_suite)
    test_dry_run_does_not_create_feedback(graded_suite, mock_ls_client)
    test_missing_runs_reported(graded_suite, mock_ls_client)
    test_feedback_key_per_suite(graded_suite)

    # Not parametrized — exercised against REVIEW + CHAT specifically.
    @pytest.mark.parametrize("suite", [REVIEW, CHAT])
    def test_writeback_raises_not_implemented(suite): ...
```

Deleted: `test_datasets.py`, `test_predictions.py`, `test_langsmith_experiments.py`, `test_inspect_helpers.py`.

Kept: `test_langsmith_routing.py` (orthogonal env-var routing concern), `test_task_wiring.py` (validates `@task` decorator surface stability across eval imports), `test_makefile.py` (validates Makefile targets resolve to current CLI verbs).

## 6. Migration order

Each step keeps `make check` green (1192 tests baseline). One commit per step.

1. Create `evals/data/_base.py`, `_experiment.py`, `_predictions.py`, `_writeback.py` by **moving** code (not re-implementing) from `runtime/{datasets,langsmith,predictions}.py`. New module-private names; no public re-export yet.
2. Create `evals/data/{review,chat,fix,swt}.py` and `evals/data/__init__.py` with the four singletons. Subclasses delegate `collect()` to copies of the existing `_collect_samples` / `_load_examples` bodies from `scripts/build_*`.
3. Add `evals/data/__main__.py` CLI. Smoke-test by running `python -m evals.data inspect <suite>` against the live LangSmith account.
4. Convert `evals/runtime/{datasets,predictions,langsmith}.py` into thin re-exports of the new module symbols. All existing imports keep working.
5. Rewrite `evals/tasks/*.py` to use `EvalDataset.build_task()`. Extract `review_martian._build_overlap_scorer` to `evals/tasks/_review_overlap_scorer.py`.
6. Rewrite `evals/Makefile` `data-*` and `writeback-*` targets to call `python -m evals.data ...`.
7. Delete `evals/scripts/build_*_dataset.py` (4 files) and `evals/scripts/writeback_*_grades.py` (2 files). Confirm no remaining references via `git grep`.
8. Delete the thin re-export files at `evals/runtime/{datasets,predictions,langsmith}.py`. By this point steps 5-7 have migrated every in-tree caller; `git grep -E 'evals\.runtime\.(datasets|predictions|langsmith)'` should return zero hits before deletion. Fix any stragglers found by `make test`.
9. Replace `tests/eval/test_{datasets,predictions,langsmith_experiments,inspect_helpers}.py` with the new `test_data_*.py` files.
10. Final `make check` + `python -m evals.data publish all --force` smoke against LangSmith dev account.

## 7. Risks

- **Migration step 5 breaks live evals** if `EvalDataset.build_task` doesn't faithfully reproduce the metadata shape Inspect / LangSmith dashboards expect. Mitigation: snapshot the pre-refactor `Task.metadata` dict for one sample per suite and assert equality in `test_build_task_returns_inspect_task`.
- **CI gating on `python -m evals.data publish all`** adds a hard prerequisite. Already true in spirit (the `build_*` scripts are required), just made explicit. Mitigation: add `make data-or-skip` target that no-ops if datasets already exist.
- **LangSmith API rate limit** during `publish all --force`. Existing chunk=100 already mitigates; the unified driver inherits it.
- **Double-source drift for fix/swt**: HF metadata pinned via `hf_revision`, LangSmith mirror written from the same fetched rows. Mitigation: `publish` records `hf_revision` in dataset description; `inspect` CLI prints it for diff against an upstream pin.

## 8. Rollback

Each migration step is one commit. `git revert` step N rolls back to step N-1. Step 1-4 are additive (no deletions); the rollback risk is concentrated in steps 5-9. The thin-reexport phase (step 4) means rollback after step 4 is safe via `git revert` of step 5+.

## 9. Out of scope (future)

- Cross-suite dashboard (a single LangSmith view of every suite's scores).
- Auto-publish on Inspect run (explicitly rejected in §4.4).
- Replacing the offline harness with online grading.
- Migrating `evals/runtime/config.py` into `evals/data/` — that's process-wide config, not data lifecycle.

# Eval Test Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add comprehensive deterministic contract tests around OpenBot eval datasets, scorers, benchmark grading, task wiring, solvers, and LangSmith experiment projection.

**Architecture:** Keep external systems stubbed and test each eval boundary at the smallest stable seam. New tests live under `tests/eval/` and exercise existing public functions plus task-construction seams without introducing live network or sandbox dependencies.

**Tech Stack:** Python, pytest, inspect-ai score/task objects, monkeypatch, lightweight fakes.

---

### Task 1: Dataset contracts

**Files:**
- Create: `tests/eval/test_datasets.py`
- Test: `evals/common/datasets.py`

- [ ] Write failing tests for review and QA example conversion, metadata merge precedence, missing/empty LangSmith datasets, and deterministic sample sorting.
- [ ] Run `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest -q tests/eval/test_datasets.py` and confirm the missing coverage fails.
- [ ] Add only the smallest helper adjustments needed if tests reveal gaps.
- [ ] Re-run the dataset tests until green.

### Task 2: Judge and scorer contracts

**Files:**
- Modify: `tests/eval/test_swe_qa_judge.py`
- Create: `tests/eval/test_swe_qa_pro.py`
- Create: `tests/eval/test_review_judge.py`
- Test: `evals/scorers/swe_qa_judge.py`
- Test: `evals/scorers/swe_qa_pro.py`
- Test: `evals/scorers/review_judge.py`

- [ ] Add failing tests for SWE-QA aggregate helpers, scorer normalization, scorer metadata, LangSmith feedback posting, review-format helpers, and invalid review-judge replies.
- [ ] Run the focused tests and confirm failures where behavior is not yet pinned.
- [ ] Make minimal code changes only if a test exposes a real contract gap.
- [ ] Re-run focused scorer tests until green.

### Task 3: SWT-Bench grading contracts

**Files:**
- Modify: `tests/eval/test_swt_bench_verified.py`
- Test: `evals/scorers/swt_bench_scorer.py`

- [ ] Add failing tests for modified-test-file extraction plus end-to-end scorer branches using mocked sandbox and parser seams:
  - success transition
  - no transition
  - rejected model patch
  - rejected gold patch
  - missing markers / missing parser signal
- [ ] Run the focused SWT tests and verify red behavior.
- [ ] Make minimal implementation changes only if a test exposes an incorrect branch.
- [ ] Re-run focused SWT tests until green.

### Task 4: Task wiring and solver contracts

**Files:**
- Create: `tests/eval/test_task_wiring.py`
- Create: `tests/eval/test_swe_solver_contracts.py`
- Test: `evals/tasks/chat_swe_qa_pro.py`
- Test: `evals/tasks/review_martian.py`
- Test: `evals/tasks/fix_swe_bench_verified.py`
- Test: `evals/tasks/test_swt_bench_verified.py`
- Test: `evals/solvers/swe_fix.py`
- Test: `evals/solvers/swe_test.py`

- [ ] Add failing tests that task constructors call the intended dataset / solver / scorer seams and propagate metadata.
- [ ] Add failing solver tests proving agent output is copied into `state.output.completion` and run config gets eval metadata.
- [ ] Run the focused tests and verify failures.
- [ ] Make minimal code changes only where the current seam is not testable or the contract is broken.
- [ ] Re-run focused task/solver tests until green.

### Task 5: LangSmith experiment bridge contracts

**Files:**
- Create: `tests/eval/test_langsmith_experiments.py`
- Test: `evals/common/langsmith_experiments.py`

- [ ] Add failing tests for no-key no-op, missing-dataset no-op, successful run + feedback posting, missing-example skip, and `_await_or_call()` type enforcement.
- [ ] Run the focused tests and verify failures.
- [ ] Make minimal code changes only if needed for explicit contract behavior.
- [ ] Re-run focused bridge tests until green.

### Task 6: Full verification

**Files:**
- Test: `tests/eval/`

- [ ] Run `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest -q tests/eval`.
- [ ] Run `UV_CACHE_DIR=/private/tmp/uv-cache uv run ruff check tests/eval evals/common evals/scorers evals/tasks evals/solvers`.
- [ ] Review the resulting test names against the design doc and close any uncovered contract gaps.

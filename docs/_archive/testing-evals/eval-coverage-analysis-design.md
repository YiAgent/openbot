# Eval Test Coverage Design

## Goal

Broaden OpenBot's eval test suite so the important eval contracts are explicit, deterministic, and resistant to architectural drift without depending on live external services or real benchmark sandboxes.

## Design principles

- Prefer component-contract tests over broad integration tests.
- Keep external systems stubbed: no live LangSmith, no real Anthropic calls, no real Docker benchmark runs.
- Test the semantic surface that matters to product correctness:
  - dataset conversion and routing
  - scorer math and scorer metadata
  - benchmark grading rules
  - task wiring
  - LangSmith projection behavior
- Preserve the repo split:
  - `evals/` contains behavior-eval implementation
  - `tests/eval/` contains deterministic contract tests

## Coverage groups

### 1. Dataset and routing contracts

Add tests for:

- `review_example_to_sample()` and `qa_example_to_sample()`
- metadata merge behavior and sample-id fallback order
- `langsmith_dataset()` failure modes for missing or empty datasets
- deterministic sorting of materialized samples
- existing public/internal routing behavior remains fail-safe

### 2. Judge and scorer contracts

Add tests for:

- `swe_qa_judge`
  - official single-message prompt envelope
  - structured JSON-schema output
  - `overall_score()` and `weighted_rl_reward()`
- `swe_qa_pro`
  - normalized `overall / 50` score
  - complete metadata projection
  - LangSmith feedback emission when a trace id exists
- `review_judge`
  - golden / candidate formatting
  - invalid judge replies become safe misses

### 3. SWT-Bench grading contracts

Extend coverage beyond the current patch-shape smoke tests:

- modified test file extraction
- success only when a model-generated test goes `FAILED|ERROR -> PASSED`
- no transition means zero
- model patch rejection means zero
- gold patch rejection means zero
- missing parser output or missing phase markers means zero

### 4. Solver and task-wiring contracts

Add lightweight tests that task constructors wire the intended surfaces together:

- `chat_swe_qa_pro_baseline`
- `review_martian_baseline_crb`
- `fix_swe_bench_verified_deepagents`
- `test_swt_bench_verified`
- `test_swt_bench_verified_deepagents`

Add solver tests for:

- `swe_fix` and `swe_test` write the agent's final message into `state.output.completion`
- LangSmith run config receives the intended eval metadata

### 5. LangSmith experiment bridge contracts

Add tests for:

- no-op start when no API key exists
- no-op start when the mirrored dataset is missing
- run + feedback emission when a matching example exists
- skipped upload when there is no matching example
- `_await_or_call()` rejects non-`Score` upstream returns

## Exclusions

This pass intentionally does **not** add:

- live LangSmith tests
- live Anthropic judge tests
- real Inspect Docker / SWE-bench / SWT-Bench sandbox runs

Those belong in smoke or integration workflows, not in deterministic unit coverage.

## Expected outcome

After this pass, changing an eval-facing contract should require an intentional test update in the same PR. The test suite should catch:

- dataset shape drift
- scorer normalization / metadata drift
- task mis-wiring
- benchmark grading regressions
- silent failures in LangSmith projection

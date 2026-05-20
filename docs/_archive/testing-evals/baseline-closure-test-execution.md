# E1 Baseline Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair eval documentation drift and turn the current deepagents review path into a durable, fully observable baseline that future OpenBot production agents can be compared against.

**Architecture:** Keep one canonical review eval surface and vary only the solver provider. Introduce a small solver registry, promote `deepagents_baseline` to a first-class provider, preserve convenience task entry points, and close the observability gaps around artifacts, LangSmith metadata, validation, and reporting. Docs must explicitly distinguish done vs partial vs deferred work.

**Tech Stack:** Python, Inspect AI, LangSmith SDK, pytest, markdown docs

---

## File map

- `docs/eval/STATE.md` — truthful milestone state and partial-task accounting
- `docs/eval/handoffs/E1-T04.md`
- `docs/eval/handoffs/E1-T06.md`
- `docs/eval/handoffs/E1-T08.md`
- `docs/eval/handoffs/E1-T09.md`
- `docs/eval/handoffs/E1-T10.md`
- `docs/eval/baseline-log.md`
- `evals/README.md` — explain baseline/prod comparison shape
- `docs/prd/openbot-eval-prd.md` — align terminology around supported baseline provider
- `evals/solvers/openbot_review.py` — keep existing deepagents implementation but rename/document as baseline provider
- `evals/solvers/registry.py` — solver ids and provider lookup
- `evals/tasks/review_martian.py` — canonical task factory + convenience task entries
- `evals/common/_metadata_spec.py`
- `evals/common/metadata.py`
- `evals/common/langsmith.py`
- `evals/common/artifacts.py`
- `scripts/validate_langsmith_run.py`
- `evals/scripts/export_run_summary.py`
- `tests/eval/test_solver_registry.py`
- `tests/eval/test_metadata.py`
- `tests/eval/test_langsmith.py`
- `tests/eval/test_artifacts.py`
- `tests/eval/test_validate_langsmith_run.py`
- `tests/eval/test_export_run_summary.py`

### Task 1: Make docs truthful about current E1 state

**Files:**
- Modify: `docs/eval/STATE.md`
- Modify: `docs/eval/handoffs/E1-T04.md`
- Modify: `docs/eval/handoffs/E1-T06.md`
- Modify: `docs/eval/handoffs/E1-T08.md`
- Modify: `docs/eval/handoffs/E1-T09.md`
- Modify: `docs/eval/handoffs/E1-T10.md`
- Modify: `docs/eval/baseline-log.md`
- Modify: `evals/README.md`
- Modify: `docs/prd/openbot-eval-prd.md`

- [ ] **Step 1: Document the three explicit states**

Add prose that distinguishes `done`, `partial`, and `deferred`, and use those labels consistently in `STATE.md` plus the affected handoffs.

- [ ] **Step 2: Correct current E1 task states**

Update the docs so the following are explicit:

```text
E1-T04 = partial (artifact implementation pending)
E1-T06 = done for deepagents_baseline provider, openbot_prod not yet implemented
E1-T08 = partial (synthetic smoke only; full LangSmith closure pending)
E1-T09 = partial (local .eval export done; LangSmith source pending)
E1-T10 = partial (routing code done; manual verification pending)
```

- [ ] **Step 3: Reframe deepagents as durable baseline**

Replace “temporary stand-in” language with “supported baseline provider” everywhere except where describing the still-missing future `openbot_prod` provider.

- [ ] **Step 4: Clarify smoke vs real baseline**

Update the README / baseline log so `martian_smoke_v1` is described as a smoke fixture, not a statistically meaningful Martian baseline.

- [ ] **Step 5: Commit**

```bash
git add docs/eval docs/prd/openbot-eval-prd.md evals/README.md
git commit -m "docs: correct eval E1 status and baseline terminology"
```

### Task 2: Introduce solver-provider abstraction for apples-to-apples comparison

**Files:**
- Create: `evals/solvers/registry.py`
- Modify: `evals/solvers/openbot_review.py`
- Modify: `evals/tasks/review_martian.py`
- Create: `tests/eval/test_solver_registry.py`

- [ ] **Step 1: Write failing registry tests**

Create tests proving:

```python
def test_deepagents_baseline_provider_is_registered(): ...
def test_unknown_solver_id_raises_clear_error(): ...
def test_review_task_metadata_records_solver_id(): ...
```

- [ ] **Step 2: Run the new tests and confirm they fail**

```bash
uv run pytest tests/eval/test_solver_registry.py -q
```

Expected: failures because `registry.py` and solver-id-aware task creation do not exist yet.

- [ ] **Step 3: Add the minimal registry**

Implement:

```python
ReviewSolverId = Literal["deepagents_baseline", "openbot_prod"]

def get_review_solver(solver_id: str): ...
```

`deepagents_baseline` resolves now; `openbot_prod` raises `NotImplementedError` with an explicit message until the production workflow exists.

- [ ] **Step 4: Refactor the task factory**

Expose:

```python
def build_review_martian_task(*, solver_id: str) -> Task: ...

@task
def review_martian_baseline() -> Task: ...

@task
def review_martian_openbot() -> Task: ...
```

All three routes must share the same dataset/scorer surface, and task metadata must include `solver_id`.

- [ ] **Step 5: Run tests to green**

```bash
uv run pytest tests/eval/test_solver_registry.py tests/eval/test_review_overlap.py -q
```

- [ ] **Step 6: Commit**

```bash
git add evals/solvers evals/tasks/review_martian.py tests/eval/test_solver_registry.py
git commit -m "feat: add review solver provider registry"
```

### Task 3: Extend metadata contracts for solver-aware comparisons

**Files:**
- Modify: `evals/common/_metadata_spec.py`
- Modify: `evals/common/metadata.py`
- Modify: `tests/eval/test_metadata.py`
- Modify: `tests/eval/test_langsmith.py`

- [ ] **Step 1: Write failing tests**

Add assertions that run metadata includes:

```python
"solver_id"
"solver_family"
```

and that `collect_run_metadata(...)` requires/returns them.

- [ ] **Step 2: Run the focused tests and confirm RED**

```bash
uv run pytest tests/eval/test_metadata.py tests/eval/test_langsmith.py -q
```

- [ ] **Step 3: Implement metadata additions**

Extend the required metadata contract and collector with:

```python
solver_id: str
solver_family: str
```

For now use:

```python
solver_family="baseline"
solver_id="deepagents_baseline"
```

when creating the baseline task metadata.

- [ ] **Step 4: Run tests to green**

```bash
uv run pytest tests/eval/test_metadata.py tests/eval/test_langsmith.py -q
```

- [ ] **Step 5: Commit**

```bash
git add evals/common tests/eval/test_metadata.py tests/eval/test_langsmith.py
git commit -m "feat: record solver identity in eval metadata"
```

### Task 4: Implement real artifact export

**Files:**
- Modify: `evals/common/artifacts.py`
- Create: `tests/eval/test_artifacts.py`
- Keep: `tests/eval/test_artifacts_live.py`

- [ ] **Step 1: Write failing unit tests**

Add tests for:

```python
def test_export_artifact_rejects_unknown_kind(): ...
def test_export_artifact_encodes_string_payload(): ...
def test_export_artifact_creates_named_run_and_returns_id(): ...
```

Use a fake client so the unit surface is deterministic.

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
uv run pytest tests/eval/test_artifacts.py -q
```

- [ ] **Step 3: Implement the minimal artifact exporter**

Replace the stub with a real implementation that:

```python
- validates ArtifactKind
- utf-8 encodes str payloads
- creates a LangSmith run named artifact::{kind}::{sample_id}
- stores payload in run inputs / attachments-compatible metadata
- returns the created run id
```

Preserve `tests/eval/test_artifacts_live.py` as the live verification layer.

- [ ] **Step 4: Run unit tests to green**

```bash
uv run pytest tests/eval/test_artifacts.py tests/eval/test_artifacts_live.py -q
```

Expected: unit tests pass; live tests skip unless credentials exist.

- [ ] **Step 5: Commit**

```bash
git add evals/common/artifacts.py tests/eval/test_artifacts.py
git commit -m "feat: implement LangSmith artifact export"
```

### Task 5: Make LangSmith validation real

**Files:**
- Modify: `scripts/validate_langsmith_run.py`
- Modify: `evals/common/langsmith.py`
- Create: `tests/eval/test_validate_langsmith_run.py`

- [ ] **Step 1: Write failing validator tests**

Add tests for:

```python
def test_validator_rejects_missing_metadata(): ...
def test_validator_rejects_bad_experiment_name(): ...
def test_validator_accepts_valid_run(): ...
```

using a fake LangSmith client / fake run object.

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
uv run pytest tests/eval/test_validate_langsmith_run.py -q
```

- [ ] **Step 3: Implement validator logic**

Move reusable checks into code that:

```python
- fetches a run
- checks required metadata fields
- checks experiment naming against the contract
- exits 0 on success, non-zero on failure
```

- [ ] **Step 4: Run tests to green**

```bash
uv run pytest tests/eval/test_validate_langsmith_run.py tests/eval/test_langsmith.py -q
```

- [ ] **Step 5: Commit**

```bash
git add scripts/validate_langsmith_run.py evals/common/langsmith.py tests/eval/test_validate_langsmith_run.py
git commit -m "feat: validate LangSmith runs against eval contract"
```

### Task 6: Improve summary export and telemetry truthfulness

**Files:**
- Modify: `evals/scripts/export_run_summary.py`
- Modify: `evals/solvers/openbot_review.py`
- Create: `tests/eval/test_export_run_summary.py`

- [ ] **Step 1: Write failing tests**

Add tests for:

```python
def test_summary_marks_usage_unavailable_instead_of_zero(): ...
def test_summary_includes_solver_identity(): ...
def test_summary_can_render_from_langsmith_payload(): ...
```

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
uv run pytest tests/eval/test_export_run_summary.py -q
```

- [ ] **Step 3: Implement minimal telemetry/reporting changes**

Adjust report generation so:

```text
- missing usage => "unavailable" rather than 0.0000
- solver_id / solver_family appear in the report
- code accepts a LangSmith-shaped payload path in addition to .eval logs
```

Where provider usage is available from deepagents/langchain callbacks, surface it into task metadata; otherwise preserve explicit “unavailable” semantics.

- [ ] **Step 4: Run focused tests to green**

```bash
uv run pytest tests/eval/test_export_run_summary.py -q
```

- [ ] **Step 5: Commit**

```bash
git add evals/scripts/export_run_summary.py evals/solvers/openbot_review.py tests/eval/test_export_run_summary.py
git commit -m "feat: make eval summaries solver-aware and telemetry-honest"
```

### Task 7: Verify the full E1 baseline slice

**Files:**
- Modify if needed: docs generated by the run

- [ ] **Step 1: Run the eval test suite**

```bash
uv run pytest tests/eval -q
```

- [ ] **Step 2: Run the smoke baseline task**

```bash
uv run inspect eval evals/tasks/review_martian.py --task review_martian_baseline --limit 5
```

- [ ] **Step 3: Export a fresh summary**

```bash
uv run python -m evals.scripts.export_run_summary <new-log-path> --out docs/reports/eval-sample-summary.md
```

- [ ] **Step 4: If LangSmith credentials are available, run live verification**

```bash
uv run pytest tests/eval/test_artifacts_live.py -q
python scripts/validate_langsmith_run.py <run_id>
```

- [ ] **Step 5: Update docs with verified status**

Only after verification, move tasks from `partial` to `done` where their original ACs are actually satisfied.

- [ ] **Step 6: Commit**

```bash
git add docs/eval docs/reports
git commit -m "docs: record verified E1 baseline closure"
```

## Self-review

- Spec coverage: docs truthfulness, durable baseline provider, shared task surface, artifact export, metadata, validator, summary/reporting, and verification are all mapped to tasks.
- Placeholder scan: no TBD/TODO placeholders remain in the plan.
- Type consistency: `solver_id` / `solver_family` are the shared identifiers used across task metadata, run metadata, and reporting.

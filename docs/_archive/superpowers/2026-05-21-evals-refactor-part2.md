# Evals Refactor Part 2: Delete Dead Files + Comment Cleanup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete 7 obsolete files, clean `agents/__init__.py`, strip comment noise from solvers/agents, verify full test suite.

**Prerequisite:** Part 1 must be complete. All 4 Tasks in part1 committed.

**Tech Stack:** Python, pytest

**Spec:** `docs/superpowers/specs/2026-05-21-evals-refactor-design.md`
**Part 1:** `docs/superpowers/plans/2026-05-21-evals-refactor-part1.md`

---

## File Map (this part)

| Action | File |
|--------|------|
| **Delete** | `evals/agents/langsmith.py` |
| **Delete** | `evals/agents/langsmith_feedback.py` |
| **Delete** | `evals/inspect/langsmith_feedback.py` |
| **Delete** | `evals/agents/convergence_middleware.py` |
| **Delete** | `evals/agents/structured_finalizer.py` |
| **Delete** | `evals/solvers/_patch_agent.py` |
| **Delete** | `tests/eval/test_patch_agent_solver.py` |
| **Modify** | `evals/agents/__init__.py` |
| **Clean** | `evals/solvers/review.py` |
| **Clean** | `evals/solvers/swe_fix.py` |
| **Clean** | `evals/solvers/swe_qa.py` |
| **Clean** | `evals/solvers/swe_test.py` |

---

### Task 5: Delete obsolete files

**Prerequisite:** Verify Part 1 is complete — `uv run pytest tests/eval/ -v` must pass.

- [ ] **Step 1: Confirm no remaining imports from the 5 old agent modules**

```bash
grep -r "from evals.agents.langsmith\b\|from evals.agents.langsmith_feedback\|from evals.agents.convergence_middleware\|from evals.agents.structured_finalizer\|from evals.solvers._patch_agent\|from evals.solvers import _patch_agent" \
    /Users/wy/projects/openbot --include="*.py" | grep -v __pycache__
```

Expected: **no output**. If any line appears, fix that import before proceeding.

- [ ] **Step 2: Delete the 7 files**

```bash
cd /Users/wy/projects/openbot
git rm evals/agents/langsmith.py
git rm evals/agents/langsmith_feedback.py
git rm evals/inspect/langsmith_feedback.py
git rm evals/agents/convergence_middleware.py
git rm evals/agents/structured_finalizer.py
git rm evals/solvers/_patch_agent.py
git rm tests/eval/test_patch_agent_solver.py
```

- [ ] **Step 3: Rewrite `evals/agents/__init__.py`**

Replace the entire file content with:

```python
from evals.agents.baseline import build_baseline_agent, build_run_config, resolve_model
from evals.agents.chat import build_chat_agent
from evals.agents.fix import build_fix_agent
from evals.agents.review import build_review_agent
from evals.agents.test_generation import build_test_generation_agent

__all__ = [
    "build_baseline_agent",
    "build_chat_agent",
    "build_fix_agent",
    "build_review_agent",
    "build_run_config",
    "build_test_generation_agent",
    "resolve_model",
]
```

- [ ] **Step 4: Run eval tests**

```bash
uv run pytest tests/eval/ -v
```

Expected: all pass. If any test fails with `ModuleNotFoundError`, find the missed import and fix it.

- [ ] **Step 5: Commit**

```bash
git add evals/agents/__init__.py
git commit -m "refactor(evals): delete 6 obsolete files + dead solver test"
```

---

### Task 6: Strip comment noise from solvers

**Files:**
- Modify: `evals/solvers/review.py`
- Modify: `evals/solvers/swe_fix.py`
- Modify: `evals/solvers/swe_qa.py`
- Modify: `evals/solvers/swe_test.py`

- [ ] **Step 1: Replace `evals/solvers/review.py` module docstring**

Replace lines 1–20 (the module docstring ending at `"""`) with:

```python
"""Inspect solver wrapping the deepagents review provider (PRD §4.1).

Input: PR diff string. Output: list[Finding] (file, line, body, severity).
The @solver shim is at the bottom of this file.
"""
```

In `_collect_raw_text`, replace the 8-line docstring with:

```python
    """Concatenate all assistant message text for safety-scanner coverage."""
```

In `deepagents_baseline_review_solver`, find the 6-line comment block starting with `# ``upstream_commit`` is martian-CRB's own snapshot SHA` and replace with:

```python
                # base_sha is the PR base commit; upstream_commit is martian-CRB's
                # snapshot SHA and must NOT be used for git checkout.
```

Also in `deepagents_baseline_review_solver`, find the 6-line comment block starting with `# Production path: clone the repo at the PR base commit` and replace with:

```python
                    # Production path: clone repo at PR base commit for full context.
```

Find and remove (delete entirely) the long comment block starting with `# Track whether *we* own the backend` (3 lines).

Find and remove the comment block `# try/finally must wrap everything after the backend is...` (4 lines). Keep the `try:` line.

Find and remove `# Structured output is enforced by the baseline...` (5-line comment inside try block). Replace with:

```python
                    # Structured output guaranteed by baseline's wrap_agent_with_finalizer.
```

- [ ] **Step 2: Replace `evals/solvers/swe_fix.py` module docstring**

Replace the module docstring (lines 1–22) with:

```python
"""SWE-bench Verified solver — deepagents inside an isolated sandbox.

The agent runs with the repo cloned at base_commit into /workspace.
git diff is captured after the run as a SweBenchPrediction.
Actual grading happens offline via the SWE-bench Docker harness.
"""
```

Remove the inline comment `# Usage aggregation lives in evals.common.usage...` (2 lines) inside the module.

Remove the comment block inside `deepagents_baseline_swe_solver` starting with `# Refuse to capture / score a diff from a run...` (6 lines). Replace with:

```python
                # Raises AgentTerminationError on incomplete runs; Inspect marks them errored.
```

- [ ] **Step 3: Replace `evals/solvers/swe_qa.py` module docstring**

Replace the module docstring (lines 1–20) with:

```python
"""SWE-QA-Pro solver — deepagents +Agent variant (Docker sandbox).

Each sample runs in its own sandbox with the repo at the pinned commit.
Final answer is schema-bound to SweQaProAnswer via the structured finalizer.
"""
```

Remove the section comment block `# ─── +Agent: Modal sandbox with repo cloned at commit ──...` and the `# Removed the closed-book...` comment block (6 lines total).

Remove the comment block inside `_invoke_agent_with_modal` starting with `# The agent is built with ``response_format=SweQaProAnswer``...` (8 lines). Replace with:

```python
    # build_chat_agent wraps with the structured finalizer (middleware.py).
```

- [ ] **Step 4: Replace `evals/solvers/swe_test.py` module docstring**

Replace the module docstring (lines 1–16) with:

```python
"""SWT-Bench Verified solver — deepagents inside an isolated sandbox.

Writes regression tests only (no production-code edits). Captures
git diff into SwtBenchPrediction; grading is offline via SWT-Bench harness.
"""
```

Remove the `# Usage aggregation lives in evals.common.usage...` comment (2 lines).

Remove the comment block inside `deepagents_baseline_swt_solver` starting with `# Same termination contract as swe_fix...` (4 lines). Replace with:

```python
                # Raises AgentTerminationError on incomplete runs.
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/eval/ -v
```

Expected: all pass (comment changes are behaviour-neutral).

- [ ] **Step 6: Commit**

```bash
git add evals/solvers/
git commit -m "refactor(evals/solvers): strip verbose module docstrings and inline rationale comments"
```

---

### Task 7: Final verification + archive

- [ ] **Step 1: Verify no `inspect_ai` imports in agents layer**

```bash
grep -r "inspect_ai" /Users/wy/projects/openbot/evals/agents/ --include="*.py"
```

Expected: **no output**. Any match is a bug.

- [ ] **Step 2: Verify no imports from deleted modules**

```bash
grep -r "from evals.agents.langsmith\b\|from evals.agents.langsmith_feedback\|from evals.agents.convergence_middleware\|from evals.agents.structured_finalizer\|_patch_agent" \
    /Users/wy/projects/openbot --include="*.py" | grep -v __pycache__
```

Expected: **no output**.

- [ ] **Step 3: Run full suite**

```bash
make check
```

Expected:

```
All checks passed.
984 passed
```

If test count differs, investigate.

- [ ] **Step 4: Archive plans and spec**

```bash
cd /Users/wy/projects/openbot
mv docs/superpowers/specs/2026-05-21-evals-refactor-design.md docs/_archive/superpowers/
mv docs/superpowers/plans/2026-05-21-evals-refactor-part1.md docs/_archive/superpowers/
mv docs/superpowers/plans/2026-05-21-evals-refactor-part2.md docs/_archive/superpowers/
git add docs/
git commit -m "chore: archive completed evals refactor plan + spec"
```

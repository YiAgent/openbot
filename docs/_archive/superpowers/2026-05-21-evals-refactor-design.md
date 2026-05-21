# Evals Refactor: Agents–Inspect AI Decoupling

**Date:** 2026-05-21  
**Status:** Approved

## Goal

Decouple `evals/agents/` from Inspect AI, consolidate fragmented files, and remove comment noise. The agents layer should have zero `inspect_ai` imports.

## Root Cause

`evals/agents/langsmith.py` imports from `inspect_ai.scorer` and `inspect_ai.solver`, pulling framework coupling into the pure-agent layer. It is also a near-duplicate of `evals/inspect/langsmith.py` (2-line diff). Four langsmith-related files exist where one suffices.

## Target Architecture

```
evals/
├── agents/               # Pure deepagents — zero inspect_ai imports
│   ├── __init__.py
│   ├── baseline.py       # resolve_model, build_baseline_agent, build_run_config
│   ├── middleware.py     # ← NEW: convergence_middleware + structured_finalizer merged
│   ├── review.py
│   ├── fix.py
│   ├── chat.py
│   └── test_generation.py
│
├── inspect/              # Inspect AI adapter layer
│   ├── __init__.py
│   ├── langsmith.py      # ← CANONICAL: LangSmithExperiment + tracing + feedback
│   ├── task_runtime.py
│   └── hf_datasets.py
│
├── solvers/              # @solver wrappers (thin adapters, unchanged structure)
├── tasks/                # @task definitions (import path updates only)
├── scorers/
├── sandboxes/
├── common/
└── scripts/
```

## Changes

### Files Deleted (6)

| File | Reason |
|------|--------|
| `evals/agents/langsmith.py` | Duplicate + inspect_ai coupling in wrong layer |
| `evals/agents/langsmith_feedback.py` | Absorbed into `inspect/langsmith.py` |
| `evals/inspect/langsmith_feedback.py` | Absorbed into `inspect/langsmith.py` |
| `evals/agents/convergence_middleware.py` | Merged into `agents/middleware.py` |
| `evals/agents/structured_finalizer.py` | Merged into `agents/middleware.py` |
| `evals/solvers/_patch_agent.py` | Unused (no imports anywhere) |

### Files Created (1)

**`evals/agents/middleware.py`** — merge of `convergence_middleware` + `structured_finalizer`:
- `ToolCallRepetitionGuard` class
- `ForceCommitBeforeBudget` class  
- `build_convergence_middlewares()` factory
- `wrap_agent_with_finalizer()` + `finalize_structured()`
- No `inspect_ai` imports (pure deepagents/langchain)

### Files Modified

| File | Change |
|------|--------|
| `evals/inspect/langsmith.py` | Absorb `ensure_feedback_config` inline; become sole canonical module; clean comments |
| `evals/agents/baseline.py` | Update internal imports: `convergence_middleware` → `middleware`, `structured_finalizer` → `middleware`; trim heavy inline docs |
| `evals/agents/__init__.py` | Remove langsmith re-exports |
| `evals/tasks/review_martian.py` | `evals.agents.langsmith` → `evals.inspect.langsmith` |
| `evals/tasks/fix_swe_bench_verified.py` | Same import fix + use `evals.inspect.hf_datasets` |
| `evals/tasks/chat_swe_qa_pro.py` | Import fix |
| `evals/tasks/test_swt_bench_verified.py` | Import fix |
| `evals/scorers/swe_qa_pro.py` | `evals.agents.langsmith_feedback` → `evals.inspect.langsmith` |
| `evals/scripts/writeback_swe_grades.py` | Same feedback import fix |
| `evals/scripts/writeback_swt_grades.py` | Same feedback import fix |
| `evals/solvers/*.py` | Trim module docstrings; remove inline design rationale comments |
| `evals/agents/*.py` | Remove excessive inline comments that duplicate docstrings |

## Decoupling Contract

After this refactor:

- `evals/agents/` → imports: `deepagents`, `langchain`, `pydantic`, `evals.common` only
- `evals/inspect/` → imports: `inspect_ai`, `langsmith`, `evals.agents`, `evals.common`
- `evals/solvers/` → imports: `inspect_ai`, `evals.agents`, `evals.inspect`, `evals.sandboxes`, `evals.common`
- `evals/tasks/` → imports: `inspect_ai`, `evals.inspect`, `evals.solvers`, `evals.common`

## Comment Cleanup Rules

Remove:
- Multi-paragraph rationale comments that repeat what the code already says
- `# Local import to avoid circular ...` comments (circular imports fixed by merge)
- Numbered rationale lists inside function bodies
- Historical context ("The previous baseline excluded...")
- Obvious one-liner annotations (`# Lazy import`, `# This is NOT official pass@1`)

Keep:
- Short docstrings explaining what a function does + args
- Non-obvious implementation choices (1-2 lines max)
- Public API docstrings

## Test Plan

```bash
make check   # fmt-check + lint + tests (984 currently passing)
```

No behavioral changes — pure structural/import refactor.

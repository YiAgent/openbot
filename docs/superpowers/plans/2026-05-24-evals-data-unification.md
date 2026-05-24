# Eval Data Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse `evals/scripts/build_*` (4 scripts), `evals/scripts/writeback_*` (2 scripts), and `evals/runtime/{datasets,predictions,langsmith}.py` (3 modules, ~1140 LOC) into one `evals/data/` package — each suite is a single `EvalDataset` singleton owning collect → publish → load → record predictions → writeback grades.

**Architecture:** ABC-based `EvalDataset` with four singletons (`REVIEW`, `CHAT`, `FIX`, `SWT`). Subclasses implement only `collect()` (upstream pull) and `example_to_sample()` (LangSmith→Inspect conversion). Base class owns chunked LangSmith publish, sha256, dataset loading, experiment lifecycle, and writeback. Single CLI `python -m evals.data <verb> <suite>` replaces six scripts. Thin re-exports preserve every existing import during migration; deletions only happen in steps 14-15 once `git grep` confirms zero callers.

**Tech Stack:** Python 3.12 · pydantic / pydantic-settings · langsmith client · datasets (HuggingFace) · inspect_ai · pytest · uv

**Spec:** `docs/superpowers/specs/2026-05-24-evals-data-unification-design.md`

---

## File Structure

### New (created in this plan)

| Path | Responsibility |
|------|----------------|
| `evals/data/__init__.py` | Re-export `EvalDataset`, `REVIEW`/`CHAT`/`FIX`/`SWT` singletons, `DATASETS` map |
| `evals/data/__main__.py` | CLI router: `python -m evals.data <verb> <suite>` |
| `evals/data/_base.py` | `EvalDataset` ABC, `CollectedExample`, `PublishResult`, `WritebackSummary`, `chunked_publish`, `sha256_examples` |
| `evals/data/_experiment.py` | `LangSmithExperiment`, `configure_tracing_for_dataset`, `ensure_feedback_config`, `git_sha`, `resolve_model_label`, `build_export_experiment` (moved verbatim from `runtime/langsmith.py`) |
| `evals/data/_predictions.py` | `SweBenchPrediction`, `SwtBenchPrediction`, `SweQaProAnswer`/`Citation`, `_AppendWriter`, `prediction_exporter`, `predictions_path`, `empty_*_prediction` (moved verbatim from `runtime/predictions.py`) |
| `evals/data/_writeback.py` | Generic `run_writeback(suite, report_path, experiment_name, dry_run, client=None)` driver — dedupes `writeback_swe_grades.py` ↔ `writeback_swt_grades.py` |
| `evals/data/review.py` | `ReviewDataset` — Martian PRs, async httpx pull, `review_example_to_sample` |
| `evals/data/chat.py` | `ChatDataset` — SWE-QA-Pro, HF pull with sha256, `qa_example_to_agent_sample` |
| `evals/data/fix.py` | `FixDataset` — SWE-bench Verified, HF mirror, `fix_example_to_sample` (new — replaces HF `load_issue_dataset` round-trip with LangSmith metadata reads) |
| `evals/data/swt.py` | `SwtDataset` — SWT-Bench Verified, HF mirror, `swt_example_to_sample` (new — same pattern as fix) |
| `evals/tasks/_review_overlap_scorer.py` | Extracted helper from `review_martian.py` so the task body shrinks |
| `tests/eval/test_data_unified.py` | Parametrized [REVIEW, CHAT, FIX, SWT] lifecycle + Inspect Task assembly tests |
| `tests/eval/test_data_predictions.py` | Schema + exporter tests (replacement for `test_predictions.py`) |
| `tests/eval/test_data_writeback.py` | Parametrized [FIX, SWT] writeback tests + REVIEW/CHAT NotImplementedError tests |
| `tests/eval/test_data_cli.py` | Argparse routing tests for `python -m evals.data` |

### Modified

| Path | Change |
|------|--------|
| `evals/runtime/datasets.py` | Step 11: thin re-export shim. Step 15: deleted. |
| `evals/runtime/predictions.py` | Step 11: thin re-export shim. Step 15: deleted. |
| `evals/runtime/langsmith.py` | Step 11: thin re-export shim. Step 15: deleted. |
| `evals/runtime/config.py` | **Unchanged** — process-wide config used by scorers, not data-only. |
| `evals/tasks/fix_swe_bench.py` | Step 12: shrink from 79 lines to ~10 via `FIX.build_task(solver=...)`. |
| `evals/tasks/test_swt_bench.py` | Step 12: shrink from 69 lines to ~10. |
| `evals/tasks/chat_swe_qa.py` | Step 12: shrink from 64 lines via `CHAT.build_task(scorer=...)`. |
| `evals/tasks/review_martian.py` | Step 12: shrink to ~25 lines, extracts overlap scorer to `_review_overlap_scorer.py`. |
| `evals/Makefile` | Step 13: rewrite `data-*` and `writeback-*` recipes to call the new CLI. |
| `tests/eval/test_makefile.py` | Step 13: assert new CLI invocations. |
| `tests/eval/test_task_wiring.py` | Step 12: monkeypatch the singleton's `build_task` instead of internals. |

### Deleted (steps 14-15)

| Path | Step |
|------|------|
| `evals/scripts/build_review_martian_dataset.py` | 14 |
| `evals/scripts/build_chat_swe_qa_pro_dataset.py` | 14 |
| `evals/scripts/build_swe_bench_verified_dataset.py` | 14 |
| `evals/scripts/build_swt_bench_verified_dataset.py` | 14 |
| `evals/scripts/writeback_swe_grades.py` | 14 |
| `evals/scripts/writeback_swt_grades.py` | 14 |
| `evals/runtime/datasets.py` | 15 |
| `evals/runtime/predictions.py` | 15 |
| `evals/runtime/langsmith.py` | 15 |
| `tests/eval/test_datasets.py` | 16 |
| `tests/eval/test_predictions.py` | 16 |
| `tests/eval/test_inspect_helpers.py` | 16 |
| `tests/eval/test_langsmith_experiments.py` | 16 |

---

## Conventions

- All commits run `make check` (`fmt-check + lint + test`) green before committing. If any test fails, fix it inside the same task — never advance with red tests.
- Use `uv run pytest <path>` not `pip install pytest`.
- Use `git mv` when moving a file to preserve history; otherwise `cp` followed by `git add`.
- New modules go under `evals/data/`. Module-private names use a leading underscore filename (`_base.py`, `_experiment.py`).
- Type annotations on every public function. `from __future__ import annotations` at the top of every new module.

---

## Tasks

### Task 1: Scaffold `evals/data/` package and `_predictions.py`

**Files:**
- Create: `evals/data/__init__.py`
- Create: `evals/data/_predictions.py`
- Test: `tests/eval/test_data_predictions.py`

Goal: move `evals/runtime/predictions.py` verbatim into `evals/data/_predictions.py` and prove it works behind a fresh test file. The original `runtime/predictions.py` stays in place this task (for back-compat); we wire the shim later.

- [ ] **Step 1: Create the package marker**

```python
# evals/data/__init__.py
"""Eval data unification package.

One module per suite, each owning its full data lifecycle: collect from
upstream → publish to LangSmith → load for Inspect → record predictions →
record experiment runs → write back offline grades.
"""

from __future__ import annotations

# Suite singletons + the ABC are wired up in Task 11. Until then this file
# only marks the package so `evals.data._predictions`/`_experiment`/`_samples`
# can be imported.
```

```bash
mkdir -p evals/data
# write the file via your editor of choice
```

- [ ] **Step 2: Copy `predictions.py` to `evals/data/_predictions.py`**

The file is copied verbatim — Task 11 will collapse the duplicate by turning `evals/runtime/predictions.py` into a thin re-export shim.

```bash
cp evals/runtime/predictions.py evals/data/_predictions.py
git add evals/data/_predictions.py
```

- [ ] **Step 3: Write a smoke test that asserts both modules expose the same public API**

```python
# tests/eval/test_data_predictions.py
"""Schema + exporter contract tests for evals/data/_predictions.py."""

from __future__ import annotations

import pytest

from evals.data import _predictions as new


PUBLIC_NAMES = (
    "SweBenchPrediction",
    "SwtBenchPrediction",
    "SweQaProAnswer",
    "SweQaProCitation",
    "empty_swe_prediction",
    "empty_swt_prediction",
    "predictions_path",
    "prediction_exporter",
)


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_public_api_exposed(name: str) -> None:
    assert hasattr(new, name), f"evals.data._predictions missing {name!r}"


def test_empty_swe_prediction_marks_unsupported() -> None:
    pred = new.empty_swe_prediction(instance_id="x", model_label="anthropic:test")
    assert pred.model_patch == ""
    assert pred.metadata.get("unsupported") is True


def test_empty_swt_prediction_marks_unsupported() -> None:
    pred = new.empty_swt_prediction(instance_id="y", model_label="anthropic:test")
    assert pred.test_patch == ""
    assert pred.metadata.get("unsupported") is True
```

- [ ] **Step 4: Run the new tests + the legacy test file (both must stay green)**

```bash
uv run pytest tests/eval/test_data_predictions.py tests/eval/test_predictions.py -v
```

Expected: all green. The new module is a verbatim copy; the old one is untouched.

- [ ] **Step 5: Run `make check`**

```bash
make check
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add evals/data/__init__.py evals/data/_predictions.py tests/eval/test_data_predictions.py
git commit -m "feat(evals/data): scaffold package and mirror predictions module"
```

---

### Task 2: Mirror `evals/runtime/langsmith.py` to `evals/data/_experiment.py`

**Files:**
- Create: `evals/data/_experiment.py`
- Test: existing `tests/eval/test_langsmith_experiments.py` and `tests/eval/test_inspect_helpers.py` import the new path via the shim wired in Task 11; for now we only assert the new module imports cleanly.

Goal: relocate `LangSmithExperiment` + helpers into `evals/data/`. We use `git mv` here because the public surface barely changes — the runtime module becomes a shim in Task 11.

- [ ] **Step 1: Copy the file (preserve history with `git mv` once the shim lands; copy first so dual-import works in Task 11)**

```bash
cp evals/runtime/langsmith.py evals/data/_experiment.py
```

- [ ] **Step 2: Update internal imports inside the new module**

Open `evals/data/_experiment.py` and rewrite the imports that reach into the data layer:

```python
# evals/data/_experiment.py — change at the top of the module
from evals.data._predictions import prediction_exporter   # was: from evals.runtime.predictions
```

`from evals.runtime.config import get_eval_config` and `from evals.runtime.predictions ...` are the only intra-eval imports — `config` stays put (Spec §4.1), so only the predictions import moves.

- [ ] **Step 3: Add a smoke import test**

```python
# tests/eval/test_data_predictions.py — append
def test_experiment_module_imports() -> None:
    from evals.data import _experiment

    for name in (
        "LangSmithExperiment",
        "configure_tracing_for_dataset",
        "ensure_feedback_config",
        "git_sha",
        "resolve_model_label",
        "build_export_experiment",
    ):
        assert hasattr(_experiment, name), f"missing {name!r}"
```

- [ ] **Step 4: Run the targeted tests**

```bash
uv run pytest tests/eval/test_data_predictions.py tests/eval/test_langsmith_experiments.py tests/eval/test_inspect_helpers.py -v
```

Expected: green. The legacy tests still target `evals.runtime.langsmith` — that's fine because we haven't shimmed it yet.

- [ ] **Step 5: Run `make check`**

```bash
make check
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add evals/data/_experiment.py tests/eval/test_data_predictions.py
git commit -m "feat(evals/data): mirror langsmith experiment helpers into _experiment.py"
```

---

### Task 3: Mirror `evals/runtime/datasets.py` helpers into `evals/data/_samples.py`

**Files:**
- Create: `evals/data/_samples.py`
- Test: append to `tests/eval/test_data_predictions.py`

Goal: relocate the LangSmith→Inspect Sample converters and `langsmith_dataset()`/`load_issue_dataset()` so the suite modules can import them. The runtime path keeps working until Task 11.

- [ ] **Step 1: Copy the module**

```bash
cp evals/runtime/datasets.py evals/data/_samples.py
```

- [ ] **Step 2: Verify the new module imports cleanly**

Append a smoke test to `tests/eval/test_data_predictions.py`:

```python
def test_samples_module_imports() -> None:
    from evals.data import _samples

    for name in (
        "review_example_to_sample",
        "qa_example_to_sample",
        "qa_example_to_agent_sample",
        "langsmith_dataset",
        "load_issue_dataset",
        "issue_row_to_sample",
        "SWE_QA_PRO_REPO_PATH",
    ):
        assert hasattr(_samples, name), f"missing {name!r}"
```

- [ ] **Step 3: Run the targeted tests**

```bash
uv run pytest tests/eval/test_data_predictions.py tests/eval/test_datasets.py -v
```

Expected: green.

- [ ] **Step 4: Run `make check`**

```bash
make check
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add evals/data/_samples.py tests/eval/test_data_predictions.py
git commit -m "feat(evals/data): mirror dataset sample converters into _samples.py"
```

---

### Task 4: Build the `EvalDataset` ABC and shared types in `evals/data/_base.py`

**Files:**
- Create: `evals/data/_base.py`
- Test: `tests/eval/test_data_unified.py` (new)

Goal: introduce the abstract base class that every suite inherits from. It owns chunked LangSmith publish, sha256 of the canonical example list, dataset loading via the new `_samples.langsmith_dataset()`, experiment startup, and a default `writeback_grades()` that raises `NotImplementedError` (overridden by FIX/SWT).

- [ ] **Step 1: Write the failing tests first**

```python
# tests/eval/test_data_unified.py
"""Lifecycle tests for EvalDataset ABC + concrete suites."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from inspect_ai.dataset import MemoryDataset, Sample

from evals.data._base import (
    CollectedExample,
    EvalDataset,
    PublishResult,
    sha256_examples,
)


class _FakeSuite(EvalDataset):
    suite: ClassVar[str] = "fake"
    dataset_version: ClassVar[str] = "fake_v1"
    instance_id_field: ClassVar[str] = "id"
    description: ClassVar[str] = "Fake suite for tests"

    def collect(self) -> list[CollectedExample]:  # type: ignore[override]
        return [
            CollectedExample(inputs={"id": "a", "x": 1}, outputs=None, metadata={}),
            CollectedExample(inputs={"id": "b", "x": 2}, outputs=None, metadata={}),
        ]

    def example_to_sample(self, example: Any) -> Sample:  # type: ignore[override]
        return Sample(id=example.inputs["id"], input=str(example.inputs["x"]))


def test_sha256_is_stable_under_key_order() -> None:
    a = [CollectedExample(inputs={"id": "1", "x": 1}, outputs=None, metadata={})]
    b = [CollectedExample(inputs={"x": 1, "id": "1"}, outputs=None, metadata={})]
    assert sha256_examples(a) == sha256_examples(b)


def test_publish_chunks_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _FakeSuite()
    seen: list[int] = []

    class _FakeClient:
        def read_dataset(self, dataset_name: str) -> Any:
            raise LookupError(dataset_name)

        def create_dataset(self, dataset_name: str, description: str) -> Any:
            return type("D", (), {"id": "00000000-0000-0000-0000-000000000000"})()

        def create_examples(self, *, inputs, outputs, metadata, dataset_id):  # type: ignore[no-untyped-def]
            seen.append(len(inputs))

    examples = [
        CollectedExample(inputs={"id": str(i), "x": i}, outputs=None, metadata={})
        for i in range(250)
    ]
    result = suite.publish(examples, client=_FakeClient(), chunk_size=100)
    assert isinstance(result, PublishResult)
    assert seen == [100, 100, 50]
    assert result.example_count == 250


def test_load_for_inspect_returns_memory_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _FakeSuite()
    from evals.data import _samples

    seen_kwargs: dict[str, Any] = {}

    def _fake_langsmith_dataset(version: str, *, converter=None) -> MemoryDataset:  # type: ignore[no-untyped-def]
        seen_kwargs["version"] = version
        seen_kwargs["converter_is_callable"] = callable(converter)
        return MemoryDataset(
            samples=[Sample(id="a", input="1"), Sample(id="b", input="2")],
            name=version,
            location="langsmith://fake",
        )

    monkeypatch.setattr(_samples, "langsmith_dataset", _fake_langsmith_dataset)
    ds = suite.load_for_inspect()
    assert [s.id for s in ds.samples] == ["a", "b"]
    assert seen_kwargs["version"] == "fake_v1"
    assert seen_kwargs["converter_is_callable"] is True


def test_writeback_grades_default_raises() -> None:
    suite = _FakeSuite()
    with pytest.raises(NotImplementedError):
        suite.writeback_grades(report_path="x", experiment_name="e")
```

- [ ] **Step 2: Run the failing test**

```bash
uv run pytest tests/eval/test_data_unified.py -v
```

Expected: FAIL with `ImportError: cannot import name 'EvalDataset' from 'evals.data._base'`.

---

- [ ] **Step 3: Write `_base.py` (top half — types + sha256 + publish helper)**

```python
# evals/data/_base.py
"""EvalDataset ABC and shared data types.

Every suite (review/chat/fix/swt) is a singleton subclass that owns the
full data lifecycle: collect upstream → publish to LangSmith → load for
Inspect → optionally writeback grades.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Iterable, TypedDict

from inspect_ai.dataset import MemoryDataset
from inspect_ai.scorer import Scorer


class CollectedExample(TypedDict):
    """The canonical row shape every `collect()` returns."""

    inputs: dict[str, Any]
    outputs: dict[str, Any] | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class PublishResult:
    dataset_id: str
    dataset_name: str
    example_count: int
    sha256: str


@dataclass(frozen=True)
class WritebackSummary:
    feedback_written: int
    runs_matched: int
    instances_unmatched: int
    dry_run: bool


def _canonical(example: CollectedExample) -> str:
    """Order-independent JSON serialisation for sha256."""

    return json.dumps(example, sort_keys=True, separators=(",", ":"))


def sha256_examples(examples: Iterable[CollectedExample]) -> str:
    digest = hashlib.sha256()
    for ex in examples:
        digest.update(_canonical(ex).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
```

---

- [ ] **Step 4: Write `_base.py` (bottom half — ABC body, publish, load, build_task)**

```python
# evals/data/_base.py — append the rest

class EvalDataset(ABC):
    """Single source of truth for a suite's data lifecycle."""

    suite: ClassVar[str]
    """Short suite key used by the CLI (`review`, `chat`, `fix`, `swt`)."""

    dataset_version: ClassVar[str]
    """Stable LangSmith dataset name (matches CatalogSettings)."""

    instance_id_field: ClassVar[str] = "id"
    """Inputs key used to recover the original LangSmith example."""

    description: ClassVar[str] = ""
    """Human-readable description recorded on the LangSmith dataset."""

    # ------------------------------------------------------------------ collect

    @abstractmethod
    def collect(self) -> list[CollectedExample]:
        """Pull the upstream rows. Pure — no LangSmith side effects."""

    @abstractmethod
    def example_to_sample(self, example: Any) -> Any:
        """Map a LangSmith Example back to an Inspect Sample."""

    # ------------------------------------------------------------------ publish

    def publish(
        self,
        examples: list[CollectedExample],
        *,
        client: Any | None = None,
        chunk_size: int = 100,
    ) -> PublishResult:
        """Upsert `examples` into the LangSmith dataset for this suite."""

        if client is None:
            from langsmith import Client

            client = Client()

        try:
            ds = client.read_dataset(dataset_name=self.dataset_version)
        except LookupError:
            ds = client.create_dataset(
                dataset_name=self.dataset_version,
                description=self.description,
            )

        for start in range(0, len(examples), chunk_size):
            batch = examples[start : start + chunk_size]
            client.create_examples(
                inputs=[ex["inputs"] for ex in batch],
                outputs=[ex.get("outputs") for ex in batch],
                metadata=[ex.get("metadata") or {} for ex in batch],
                dataset_id=str(ds.id),
            )

        return PublishResult(
            dataset_id=str(ds.id),
            dataset_name=self.dataset_version,
            example_count=len(examples),
            sha256=sha256_examples(examples),
        )

    # ------------------------------------------------------------------- load

    def load_for_inspect(self) -> MemoryDataset:
        """Load the published dataset as an Inspect MemoryDataset.

        Uses the suite's `example_to_sample` to convert each LangSmith
        Example back into an Inspect Sample; subclasses don't need to
        override this method unless they need a non-LangSmith source.
        """

        from evals.data import _samples

        return _samples.langsmith_dataset(
            self.dataset_version,
            converter=self.example_to_sample,
        )

    # ------------------------------------------------------------- writeback

    def writeback_grades(
        self,
        *,
        report_path: str,
        experiment_name: str,
        dry_run: bool = False,
        client: Any | None = None,
    ) -> WritebackSummary:
        """Default: review/chat have online grading, no writeback."""

        raise NotImplementedError(
            f"{type(self).__name__} does not support offline grade writeback."
        )

    # ----------------------------------------------------------- build_task

    def build_task(
        self,
        *,
        solver: Any,
        scorer: Scorer | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Hook for suite subclasses; must be overridden."""

        raise NotImplementedError(
            f"{type(self).__name__} must override build_task()."
        )
```

- [ ] **Step 5: Run the test from Step 1**

```bash
uv run pytest tests/eval/test_data_unified.py -v
```

Expected: PASS.

- [ ] **Step 6: Run `make check`**

```bash
make check
```

Expected: green.

- [ ] **Step 7: Commit**

```bash
git add evals/data/_base.py tests/eval/test_data_unified.py
git commit -m "feat(evals/data): introduce EvalDataset ABC with publish/load/sha256"
```

---

### Task 5: Generic writeback driver in `evals/data/_writeback.py`

**Files:**
- Create: `evals/data/_writeback.py`
- Test: `tests/eval/test_data_writeback.py` (new)

Goal: dedupe `evals/scripts/writeback_swe_grades.py` (~238 lines) and `evals/scripts/writeback_swt_grades.py` (~271 lines). The two only differ in (a) feedback key, (b) report-row schema, (c) what counts as "resolved". Capture those three knobs on the suite class and let `_writeback.run_writeback(suite, ...)` drive the rest.

- [ ] **Step 1: Write the failing test for the FIX writeback**

```python
# tests/eval/test_data_writeback.py
"""Parametrized writeback tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from evals.data import FIX, SWT
from evals.data._base import WritebackSummary
from evals.data._writeback import run_writeback


@pytest.mark.parametrize("suite", [FIX, SWT])
def test_writeback_creates_one_feedback_per_resolved_run(
    suite, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        '{"resolved": ["repo__inst-1"], "unresolved": [], "error": []}',
        encoding="utf-8",
    )

    feedbacks: list[dict[str, Any]] = []

    class _FakeClient:
        def list_runs(self, **kwargs):  # type: ignore[no-untyped-def]
            return iter([
                SimpleNamespace(id="run-1", extra={"metadata": {"instance_id": "repo__inst-1"}})
            ])

        def create_feedback(self, **kwargs):  # type: ignore[no-untyped-def]
            feedbacks.append(kwargs)

    summary = run_writeback(
        suite=suite,
        report_path=str(report_path),
        experiment_name="exp-1",
        dry_run=False,
        client=_FakeClient(),
    )

    assert isinstance(summary, WritebackSummary)
    assert summary.feedback_written == 1
    assert summary.runs_matched == 1
    assert feedbacks[0]["score"] == 1.0
    assert feedbacks[0]["key"] == suite.feedback_key


def test_writeback_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        '{"resolved": ["repo__inst-1"], "unresolved": [], "error": []}',
        encoding="utf-8",
    )

    class _FakeClient:
        def list_runs(self, **kwargs):  # type: ignore[no-untyped-def]
            return iter([
                SimpleNamespace(id="r", extra={"metadata": {"instance_id": "repo__inst-1"}})
            ])

        def create_feedback(self, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("dry_run must not write")

    summary = run_writeback(
        suite=FIX,
        report_path=str(report_path),
        experiment_name="exp",
        dry_run=True,
        client=_FakeClient(),
    )

    assert summary.dry_run is True
    assert summary.feedback_written == 0
```

- [ ] **Step 2: Run the failing test**

```bash
uv run pytest tests/eval/test_data_writeback.py -v
```

Expected: FAIL — the FIX/SWT singletons and `run_writeback` don't exist yet.

---

- [ ] **Step 3: Implement `_writeback.py`**

```python
# evals/data/_writeback.py
"""Generic offline-grade writeback driver.

Replaces the duplicated `writeback_swe_grades.py` / `writeback_swt_grades.py`
scripts. The suite class supplies (a) `feedback_key` and (b) a static
`classify(report)` that yields `(instance_id, score, comment)` triples.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Protocol

from evals.data._base import WritebackSummary


class _WritebackSuite(Protocol):
    feedback_key: str

    @staticmethod
    def classify(report: dict[str, Any]) -> Iterable[tuple[str, float, str]]: ...


def _load_report(report_path: str) -> dict[str, Any]:
    return json.loads(Path(report_path).read_text(encoding="utf-8"))


def _runs_by_instance(client: Any, *, experiment_name: str) -> dict[str, list[Any]]:
    runs: dict[str, list[Any]] = {}
    for run in client.list_runs(project_name=experiment_name, execution_order=1):
        instance_id = (run.extra or {}).get("metadata", {}).get("instance_id")
        if instance_id is None:
            continue
        runs.setdefault(instance_id, []).append(run)
    return runs


def run_writeback(
    *,
    suite: _WritebackSuite,
    report_path: str,
    experiment_name: str,
    dry_run: bool = False,
    client: Any | None = None,
) -> WritebackSummary:
    """Stream a Docker-harness report into LangSmith feedback rows."""

    if client is None:
        from langsmith import Client

        client = Client()

    report = _load_report(report_path)
    runs = _runs_by_instance(client, experiment_name=experiment_name)

    written = 0
    matched = 0
    unmatched = 0
    for instance_id, score, comment in suite.classify(report):
        instance_runs = runs.get(instance_id, [])
        if not instance_runs:
            unmatched += 1
            continue
        matched += 1
        if dry_run:
            continue
        for run in instance_runs:
            client.create_feedback(
                run_id=str(run.id),
                key=suite.feedback_key,
                score=score,
                comment=comment,
            )
            written += 1

    return WritebackSummary(
        feedback_written=written,
        runs_matched=matched,
        instances_unmatched=unmatched,
        dry_run=dry_run,
    )
```

- [ ] **Step 4: Tests will pass after Tasks 8/9 wire `feedback_key` + `classify` on FIX/SWT**

This task only delivers the driver. Re-run after Task 9 to confirm parametrized writeback tests pass.

- [ ] **Step 5: Run `make check`**

```bash
make check
```

Expected: green (the new test file is xfail until Task 9 — mark with `pytest.mark.skip` here, removed in Task 9).

Mark the test:

```python
# tests/eval/test_data_writeback.py — add at top
pytestmark = pytest.mark.skip(reason="enabled in Task 9 once FIX/SWT singletons land")
```

- [ ] **Step 6: Commit**

```bash
git add evals/data/_writeback.py tests/eval/test_data_writeback.py
git commit -m "feat(evals/data): add generic writeback driver"
```

---

### Task 6: `ReviewDataset` — Martian PRs

**Files:**
- Create: `evals/data/review.py`
- Test: append parametrized cases to `tests/eval/test_data_unified.py`

Goal: port `evals/scripts/build_review_martian_dataset.py` (~271 lines) into a `ReviewDataset(EvalDataset)` subclass. The async httpx pull moves into `collect()`. The `review_example_to_sample` converter moves out of `_samples.py` into the suite.

- [ ] **Step 1: Implement `ReviewDataset`**

```python
# evals/data/review.py
"""Martian-CRB PR review dataset (E2 in PRD §3.1)."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from inspect_ai import Task
from inspect_ai.scorer import Scorer

from evals.data._base import CollectedExample, EvalDataset
from evals.data._samples import review_example_to_sample


class ReviewDataset(EvalDataset):
    suite: ClassVar[str] = "review"
    dataset_version: ClassVar[str] = "martian_2026w20"
    instance_id_field: ClassVar[str] = "id"
    description: ClassVar[str] = "Martian-CRB PR review prompts"

    # ----- collect -----
    def collect(self) -> list[CollectedExample]:
        return asyncio.run(self._collect_async())

    async def _collect_async(self) -> list[CollectedExample]:
        # Transitional import — `_collect_samples` is private inside the
        # build script we are about to delete. Task 14, Step 3 inlines the
        # full body here so this `evals.scripts` reference disappears with
        # the script.
        from evals.scripts.build_review_martian_dataset import (
            _collect_samples,
            _row_to_example,
            _sha256_of_samples,
        )

        samples = await _collect_samples()
        sha256 = _sha256_of_samples(samples)
        return [
            CollectedExample(
                inputs=_row_to_example(row, sha256)["inputs"],
                outputs=_row_to_example(row, sha256).get("outputs"),
                metadata=_row_to_example(row, sha256).get("metadata", {}),
            )
            for row in samples
        ]

    # ----- example_to_sample -----
    def example_to_sample(self, example: Any) -> Any:
        return review_example_to_sample(example)

    # ----- build_task -----
    def build_task(
        self,
        *,
        solver: Any,
        scorer: Scorer | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Task:
        from inspect_ai.scorer import mean, stderr

        from evals.data._experiment import (
            LangSmithExperiment,
            configure_tracing_for_dataset,
        )
        from evals.runtime.config import get_eval_config

        if scorer is None:
            raise ValueError("ReviewDataset.build_task requires an explicit scorer")

        catalog = get_eval_config().catalog
        configure_tracing_for_dataset(self.dataset_version)
        experiment = LangSmithExperiment.start(
            dataset_name=self.dataset_version,
            solver_family=catalog.solver_family_baseline,
            instance_id_field=self.instance_id_field,
        )
        wrapped = experiment.wrap(
            scorer,
            metrics=[mean(), stderr()],
            scorer_name="review_overlap_f1",
            feedback_key="review_overlap_f1",
            feedback_config=catalog.unit_feedback_config,
        )
        metadata = {
            "dataset_version": self.dataset_version,
            "solver_family": catalog.solver_family_baseline,
            **experiment.metadata(),
            **(extra_metadata or {}),
        }
        return Task(
            dataset=self.load_for_inspect(),
            solver=solver,
            scorer=wrapped,
            metadata=metadata,
        )
```

- [ ] **Step 2: Append to the unified test**

```python
# tests/eval/test_data_unified.py — append
from evals.data.review import ReviewDataset


def test_review_dataset_class_attrs() -> None:
    assert ReviewDataset.suite == "review"
    assert ReviewDataset.dataset_version == "martian_2026w20"
    assert ReviewDataset.instance_id_field == "id"
```

- [ ] **Step 3: Run targeted tests**

```bash
uv run pytest tests/eval/test_data_unified.py -v
```

Expected: PASS.

- [ ] **Step 4: Run `make check`**

```bash
make check
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add evals/data/review.py tests/eval/test_data_unified.py
git commit -m "feat(evals/data): add ReviewDataset suite class"
```

---

### Task 7: `ChatDataset` — SWE-QA-Pro

**Files:**
- Create: `evals/data/chat.py`
- Test: append to `tests/eval/test_data_unified.py`

Goal: port `evals/scripts/build_chat_swe_qa_pro_dataset.py` into a `ChatDataset(EvalDataset)` class. `collect()` reads HF, applies sha256, and returns examples; `example_to_sample()` calls `qa_example_to_agent_sample`.

- [ ] **Step 1: Implement `ChatDataset`**

```python
# evals/data/chat.py
"""SWE-QA-Pro chat-mode dataset (E1.b in PRD §3.1)."""

from __future__ import annotations

from typing import Any, ClassVar

from inspect_ai import Task
from inspect_ai.scorer import Scorer

from evals.data._base import CollectedExample, EvalDataset
from evals.data._samples import qa_example_to_agent_sample


class ChatDataset(EvalDataset):
    suite: ClassVar[str] = "chat"
    dataset_version: ClassVar[str] = "chat_swe_qa_pro_v1"
    instance_id_field: ClassVar[str] = "id"
    description: ClassVar[str] = "SWE-QA-Pro grounded chat tasks"
    hf_dataset: ClassVar[str] = "TIGER-Lab/SWE-QA-Pro-Bench"

    def collect(self) -> list[CollectedExample]:
        # Transitional — Task 14, Step 3 inlines this so the `evals.scripts`
        # import disappears with the deleted script.
        from evals.scripts.build_chat_swe_qa_pro_dataset import (
            HF_REVISION,
            _collect_samples,
            _row_to_example,
            _sha256_of_samples,
        )

        revision = HF_REVISION
        samples = _collect_samples(revision)
        sha256 = _sha256_of_samples(samples)
        return [
            CollectedExample(
                inputs=_row_to_example(row, sha256, revision)["inputs"],
                outputs=_row_to_example(row, sha256, revision).get("outputs"),
                metadata=_row_to_example(row, sha256, revision).get("metadata", {}),
            )
            for row in samples
        ]

    def example_to_sample(self, example: Any) -> Any:
        return qa_example_to_agent_sample(example)

    def build_task(
        self,
        *,
        solver: Any,
        scorer: Scorer | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Task:
        from inspect_ai.scorer import mean, stderr

        from evals.data._experiment import (
            LangSmithExperiment,
            configure_tracing_for_dataset,
        )
        from evals.runtime.config import get_eval_config

        if scorer is None:
            raise ValueError("ChatDataset.build_task requires an explicit scorer")

        catalog = get_eval_config().catalog
        configure_tracing_for_dataset(self.dataset_version)
        experiment = LangSmithExperiment.start(
            dataset_name=self.dataset_version,
            solver_family=catalog.solver_family_baseline,
            instance_id_field=self.instance_id_field,
        )
        wrapped = experiment.wrap(
            scorer,
            metrics=[mean(), stderr()],
            scorer_name="swe_qa_pro_judge",
            feedback_key="swe_qa_pro_judge",
            feedback_config=catalog.unit_feedback_config,
        )
        metadata = {
            "dataset_version": self.dataset_version,
            "dataset_source": catalog.chat.dataset_source,
            "solver_family": catalog.solver_family_baseline,
            **experiment.metadata(),
            **(extra_metadata or {}),
        }
        return Task(
            dataset=self.load_for_inspect(),
            solver=solver,
            scorer=wrapped,
            metadata=metadata,
        )
```

- [ ] **Step 2: Add the contract test**

```python
# tests/eval/test_data_unified.py — append
from evals.data.chat import ChatDataset


def test_chat_dataset_class_attrs() -> None:
    assert ChatDataset.suite == "chat"
    assert ChatDataset.dataset_version == "chat_swe_qa_pro_v1"
    assert ChatDataset.hf_dataset == "TIGER-Lab/SWE-QA-Pro-Bench"
```

- [ ] **Step 3: Run + `make check`**

```bash
uv run pytest tests/eval/test_data_unified.py -v
make check
```

Expected: green.

- [ ] **Step 4: Commit**

```bash
git add evals/data/chat.py tests/eval/test_data_unified.py
git commit -m "feat(evals/data): add ChatDataset suite class"
```

---

### Task 8: `FixDataset` — SWE-bench Verified

**Files:**
- Create: `evals/data/fix.py`
- Test: append to `tests/eval/test_data_unified.py`

Goal: port `evals/scripts/build_swe_bench_verified_dataset.py` (HF→LangSmith mirror) AND extract the FIX-side `classify(report)` for the writeback driver. `build_task()` wires `prediction_exporter` + `build_export_experiment` (no scorer overlay — grading is offline via Docker harness).

- [ ] **Step 1: Implement `FixDataset`**

```python
# evals/data/fix.py
"""SWE-bench Verified fix dataset (E3 in PRD §3.1)."""

from __future__ import annotations

from typing import Any, ClassVar, Iterable

from inspect_ai import Task
from inspect_ai.scorer import Scorer

from evals.data._base import CollectedExample, EvalDataset, WritebackSummary
from evals.data._predictions import SweBenchPrediction
from evals.data._samples import issue_row_to_sample
from evals.data._writeback import run_writeback


class FixDataset(EvalDataset):
    suite: ClassVar[str] = "fix"
    dataset_version: ClassVar[str] = "fix_swe_bench_verified"
    hf_dataset: ClassVar[str] = "princeton-nlp/SWE-bench_Verified"
    instance_id_field: ClassVar[str] = "instance_id"
    description: ClassVar[str] = "SWE-bench Verified fix instances"
    feedback_key: ClassVar[str] = "swe_bench_pass_at_1"

    # ----- collect -----
    def collect(self) -> list[CollectedExample]:
        from datasets import load_dataset

        rows = load_dataset(self.hf_dataset, split="test")
        return [
            CollectedExample(
                inputs={
                    "instance_id": row["instance_id"],
                    "problem_statement": row["problem_statement"],
                    "repo": row["repo"],
                    "base_commit": row["base_commit"],
                    "version": row.get("version", ""),
                    "FAIL_TO_PASS": row.get("FAIL_TO_PASS", []),
                },
                outputs={"patch": row.get("patch", "")},
                metadata={"hf_dataset": self.hf_dataset},
            )
            for row in rows
        ]

    # ----- example_to_sample -----
    def example_to_sample(self, example: Any) -> Any:
        # LangSmith Examples surface `.inputs` as either a dict or a JSON-able
        # attribute; reuse the existing coercion + issue-row converter.
        from evals.data._samples import _coerce_attr  # type: ignore[attr-defined]

        return issue_row_to_sample(_coerce_attr(example, "inputs"))

    # ----- writeback classifier (consumed by run_writeback) -----
    @staticmethod
    def classify(report: dict[str, Any]) -> Iterable[tuple[str, float, str]]:
        for instance_id in report.get("resolved", []):
            yield (instance_id, 1.0, "swe-bench resolved")
        for instance_id in report.get("unresolved", []):
            yield (instance_id, 0.0, "swe-bench unresolved")
        for instance_id in report.get("error", []):
            yield (instance_id, 0.0, "swe-bench error")

    def writeback_grades(
        self,
        *,
        report_path: str,
        experiment_name: str,
        dry_run: bool = False,
        client: Any | None = None,
    ) -> WritebackSummary:
        return run_writeback(
            suite=self,
            report_path=report_path,
            experiment_name=experiment_name,
            dry_run=dry_run,
            client=client,
        )

    # ----- build_task -----
    def build_task(
        self,
        *,
        solver: Any,
        scorer: Scorer | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Task:
        from evals.data._experiment import (
            build_export_experiment,
            configure_tracing_for_dataset,
            git_sha,
            resolve_model_label,
        )
        from evals.runtime.config import get_eval_config

        catalog = get_eval_config().catalog
        configure_tracing_for_dataset(self.dataset_version)
        sha = git_sha()
        model_label = resolve_model_label()

        exp = build_export_experiment(
            dataset_version=self.dataset_version,
            solver_family=catalog.solver_family_baseline,
            model=model_label,
            git_sha=sha,
            schema=SweBenchPrediction,
            scorer_name=catalog.swe_export_feedback_key,
            feedback_key=catalog.swe_export_feedback_key,
        )
        metadata = {
            "dataset_version": self.dataset_version,
            "dataset_source": f"huggingface:{self.hf_dataset}",
            "git_sha": sha,
            "solver_family": catalog.solver_family_baseline,
            "model": model_label,
            **exp.metadata,
            **(extra_metadata or {}),
        }
        return Task(
            dataset=self.load_for_inspect(),
            solver=solver,
            scorer=exp.scorer,
            metadata=metadata,
        )
```

- [ ] **Step 2: Append tests**

```python
# tests/eval/test_data_unified.py — append
from evals.data.fix import FixDataset


def test_fix_dataset_class_attrs() -> None:
    assert FixDataset.suite == "fix"
    assert FixDataset.feedback_key == "swe_bench_pass_at_1"


def test_fix_classify_emits_score_per_instance() -> None:
    report = {"resolved": ["a"], "unresolved": ["b"], "error": ["c"]}
    triples = list(FixDataset.classify(report))
    scores = {iid: score for iid, score, _ in triples}
    assert scores == {"a": 1.0, "b": 0.0, "c": 0.0}
```

- [ ] **Step 3: Run + `make check`**

```bash
uv run pytest tests/eval/test_data_unified.py -v
make check
```

Expected: green.

- [ ] **Step 4: Commit**

```bash
git add evals/data/fix.py tests/eval/test_data_unified.py
git commit -m "feat(evals/data): add FixDataset suite class"
```

---

### Task 9: `SwtDataset` — SWT-Bench Verified

**Files:**
- Create: `evals/data/swt.py`
- Test: append to `tests/eval/test_data_unified.py`; un-skip `tests/eval/test_data_writeback.py`.

Goal: `FixDataset` twin for SWT-Bench. The shape is identical except for (a) the prediction schema (`SwtBenchPrediction`), (b) the feedback key (`swt_bench_pass_at_1`), (c) HF dataset path, and (d) the resolved/unresolved field names in the report (SWT uses `to_resolved` / `to_unresolved` in some report variants — copy whatever `writeback_swt_grades.py` uses today).

- [ ] **Step 1: Implement `SwtDataset`**

```python
# evals/data/swt.py
"""SWT-Bench Verified test-generation dataset (E4 in PRD §3.1)."""

from __future__ import annotations

from typing import Any, ClassVar, Iterable

from inspect_ai import Task
from inspect_ai.scorer import Scorer

from evals.data._base import CollectedExample, EvalDataset, WritebackSummary
from evals.data._predictions import SwtBenchPrediction
from evals.data._samples import issue_row_to_sample
from evals.data._writeback import run_writeback


class SwtDataset(EvalDataset):
    suite: ClassVar[str] = "swt"
    dataset_version: ClassVar[str] = "test_swt_bench_verified"
    hf_dataset: ClassVar[str] = "eth-sri/SWT-bench_Verified_bm25_27k_zsb"
    instance_id_field: ClassVar[str] = "instance_id"
    description: ClassVar[str] = "SWT-Bench Verified test-generation instances"
    feedback_key: ClassVar[str] = "swt_bench_pass_at_1"

    def collect(self) -> list[CollectedExample]:
        from datasets import load_dataset

        rows = load_dataset(self.hf_dataset, split="test")
        return [
            CollectedExample(
                inputs={
                    "instance_id": row["instance_id"],
                    "problem_statement": row["problem_statement"],
                    "repo": row["repo"],
                    "base_commit": row["base_commit"],
                    "version": row.get("version", ""),
                    "FAIL_TO_PASS": row.get("FAIL_TO_PASS", []),
                },
                outputs={"test_patch": row.get("test_patch", "")},
                metadata={"hf_dataset": self.hf_dataset},
            )
            for row in rows
        ]

    def example_to_sample(self, example: Any) -> Any:
        from evals.data._samples import _coerce_attr  # type: ignore[attr-defined]

        return issue_row_to_sample(_coerce_attr(example, "inputs"))

    @staticmethod
    def classify(report: dict[str, Any]) -> Iterable[tuple[str, float, str]]:
        for instance_id in report.get("resolved", []):
            yield (instance_id, 1.0, "swt-bench resolved")
        for instance_id in report.get("unresolved", []):
            yield (instance_id, 0.0, "swt-bench unresolved")
        for instance_id in report.get("error", []):
            yield (instance_id, 0.0, "swt-bench error")

    def writeback_grades(
        self,
        *,
        report_path: str,
        experiment_name: str,
        dry_run: bool = False,
        client: Any | None = None,
    ) -> WritebackSummary:
        return run_writeback(
            suite=self,
            report_path=report_path,
            experiment_name=experiment_name,
            dry_run=dry_run,
            client=client,
        )

    def build_task(
        self,
        *,
        solver: Any,
        scorer: Scorer | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Task:
        from evals.data._experiment import (
            build_export_experiment,
            configure_tracing_for_dataset,
            git_sha,
            resolve_model_label,
        )
        from evals.runtime.config import get_eval_config

        catalog = get_eval_config().catalog
        configure_tracing_for_dataset(self.dataset_version)
        sha = git_sha()
        model_label = resolve_model_label()

        exp = build_export_experiment(
            dataset_version=self.dataset_version,
            solver_family=catalog.solver_family_baseline,
            model=model_label,
            git_sha=sha,
            schema=SwtBenchPrediction,
            scorer_name=catalog.swt_export_feedback_key,
            feedback_key=catalog.swt_export_feedback_key,
        )
        metadata = {
            "dataset_version": self.dataset_version,
            "dataset_source": f"huggingface:{self.hf_dataset}",
            "git_sha": sha,
            "solver_family": catalog.solver_family_baseline,
            "model": model_label,
            **exp.metadata,
            **(extra_metadata or {}),
        }
        return Task(
            dataset=self.load_for_inspect(),
            solver=solver,
            scorer=exp.scorer,
            metadata=metadata,
        )
```

- [ ] **Step 2: Add tests**

```python
# tests/eval/test_data_unified.py — append
from evals.data.swt import SwtDataset


def test_swt_dataset_class_attrs() -> None:
    assert SwtDataset.suite == "swt"
    assert SwtDataset.feedback_key == "swt_bench_pass_at_1"


def test_swt_classify_emits_score_per_instance() -> None:
    report = {"resolved": ["x"], "unresolved": ["y"], "error": []}
    scores = {iid: score for iid, score, _ in SwtDataset.classify(report)}
    assert scores == {"x": 1.0, "y": 0.0}
```

- [ ] **Step 3: Un-skip the writeback test added in Task 5**

```python
# tests/eval/test_data_writeback.py — DELETE the line:
# pytestmark = pytest.mark.skip(reason="enabled in Task 9 once FIX/SWT singletons land")
```

- [ ] **Step 4: Run + `make check`**

```bash
uv run pytest tests/eval/test_data_unified.py tests/eval/test_data_writeback.py -v
make check
```

Expected: green for both files.

- [ ] **Step 5: Commit**

```bash
git add evals/data/swt.py tests/eval/test_data_unified.py tests/eval/test_data_writeback.py
git commit -m "feat(evals/data): add SwtDataset suite class and enable writeback tests"
```

---

### Task 10: CLI router `python -m evals.data <verb> <suite>`

**Files:**
- Create: `evals/data/__main__.py`
- Test: `tests/eval/test_data_cli.py` (new)

Goal: a single argparse front door that drives `collect`, `publish`, `refresh` (collect → publish), and `writeback`. This replaces six scripts.

- [ ] **Step 1: Write the failing test**

```python
# tests/eval/test_data_cli.py
"""Argparse routing tests for `python -m evals.data`."""

from __future__ import annotations

from typing import Any

import pytest

from evals.data import __main__ as cli


def test_publish_invokes_suite_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class _StubSuite:
        suite = "fix"
        dataset_version = "v1"

        def collect(self):  # type: ignore[no-untyped-def]
            calls["collected"] = True
            return [{"inputs": {"id": "1"}, "outputs": None, "metadata": {}}]

        def publish(self, examples, *, client=None, chunk_size=100):  # type: ignore[no-untyped-def]
            calls["published"] = (len(examples), chunk_size)
            return type("R", (), {"example_count": len(examples), "sha256": "x" * 64, "dataset_id": "d", "dataset_name": "v1"})()

    monkeypatch.setattr(cli, "_resolve_suite", lambda name: _StubSuite())
    cli.main(["refresh", "fix"])
    assert calls == {"collected": True, "published": (1, 100)}


def test_unknown_suite_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        cli.main(["refresh", "nope"])
```

- [ ] **Step 2: Implement `__main__.py`**

```python
# evals/data/__main__.py
"""CLI front-door for the unified eval data layer."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from evals.data._base import EvalDataset


def _resolve_suite(name: str) -> EvalDataset:
    from evals.data import DATASETS

    try:
        return DATASETS[name]
    except KeyError:
        raise SystemExit(f"unknown suite: {name!r}; expected one of {sorted(DATASETS)}")


def _cmd_collect(args: argparse.Namespace) -> int:
    suite = _resolve_suite(args.suite)
    examples = suite.collect()
    json.dump(list(examples), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    suite = _resolve_suite(args.suite)
    examples = json.loads(sys.stdin.read())
    result = suite.publish(examples)
    print(f"published {result.example_count} examples sha256={result.sha256}")
    return 0


def _cmd_refresh(args: argparse.Namespace) -> int:
    suite = _resolve_suite(args.suite)
    examples = suite.collect()
    result = suite.publish(examples)
    print(f"refreshed {suite.suite}: {result.example_count} examples sha256={result.sha256}")
    return 0


def _cmd_writeback(args: argparse.Namespace) -> int:
    suite = _resolve_suite(args.suite)
    summary = suite.writeback_grades(
        report_path=args.report,
        experiment_name=args.experiment,
        dry_run=args.dry_run,
    )
    print(f"writeback {suite.suite}: {summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.data")
    sub = parser.add_subparsers(dest="cmd", required=True)

    collect = sub.add_parser("collect", help="Pull upstream rows and emit JSON to stdout.")
    collect.add_argument("suite")
    collect.set_defaults(func=_cmd_collect)

    publish = sub.add_parser("publish", help="Read JSON examples on stdin, upsert into LangSmith.")
    publish.add_argument("suite")
    publish.set_defaults(func=_cmd_publish)

    refresh = sub.add_parser("refresh", help="collect → publish in one step.")
    refresh.add_argument("suite")
    refresh.set_defaults(func=_cmd_refresh)

    writeback = sub.add_parser("writeback", help="Stream a Docker harness report into feedback.")
    writeback.add_argument("suite")
    writeback.add_argument("--report", required=True, help="Path to the harness report JSON.")
    writeback.add_argument("--experiment", required=True, help="LangSmith experiment name.")
    writeback.add_argument("--dry-run", action="store_true")
    writeback.set_defaults(func=_cmd_writeback)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

- [ ] **Step 3: Run + `make check`**

```bash
uv run pytest tests/eval/test_data_cli.py -v
make check
```

Expected: green.

- [ ] **Step 4: Commit**

```bash
git add evals/data/__main__.py tests/eval/test_data_cli.py
git commit -m "feat(evals/data): add CLI front-door (collect/publish/refresh/writeback)"
```

---

### Task 11: Wire singletons + collapse runtime modules to thin re-exports

**Files:**
- Modify: `evals/data/__init__.py`
- Modify: `evals/runtime/predictions.py`
- Modify: `evals/runtime/langsmith.py`
- Modify: `evals/runtime/datasets.py`
- Test: existing legacy tests must keep passing through the shim

Goal: surface the four singletons + a `DATASETS` dict on the package; turn the three `runtime/` modules into ~5-line re-export shims. After this task, every existing import path keeps working — but everyone is reading from `evals.data`.

- [ ] **Step 1: Wire `evals/data/__init__.py`**

```python
# evals/data/__init__.py
"""Eval data unification package."""

from __future__ import annotations

from evals.data._base import (
    CollectedExample,
    EvalDataset,
    PublishResult,
    WritebackSummary,
    sha256_examples,
)
from evals.data.chat import ChatDataset
from evals.data.fix import FixDataset
from evals.data.review import ReviewDataset
from evals.data.swt import SwtDataset

REVIEW = ReviewDataset()
CHAT = ChatDataset()
FIX = FixDataset()
SWT = SwtDataset()

DATASETS: dict[str, EvalDataset] = {
    REVIEW.suite: REVIEW,
    CHAT.suite: CHAT,
    FIX.suite: FIX,
    SWT.suite: SWT,
}

__all__ = [
    "CollectedExample",
    "DATASETS",
    "EvalDataset",
    "PublishResult",
    "WritebackSummary",
    "REVIEW",
    "CHAT",
    "FIX",
    "SWT",
    "ReviewDataset",
    "ChatDataset",
    "FixDataset",
    "SwtDataset",
    "sha256_examples",
]
```

- [ ] **Step 2: Replace `evals/runtime/predictions.py` with a re-export shim**

```python
# evals/runtime/predictions.py
"""Deprecated: use `evals.data._predictions`. Kept for back-compat — Task 15 deletes this file."""

from __future__ import annotations

from evals.data._predictions import *  # noqa: F401,F403
from evals.data._predictions import (  # noqa: F401  re-export private names callers use
    SweBenchPrediction,
    SweQaProAnswer,
    SweQaProCitation,
    SwtBenchPrediction,
    empty_swe_prediction,
    empty_swt_prediction,
    prediction_exporter,
    predictions_path,
)
```

- [ ] **Step 3: Replace `evals/runtime/langsmith.py` with a shim**

```python
# evals/runtime/langsmith.py
"""Deprecated: use `evals.data._experiment`. Removed in Task 15."""

from __future__ import annotations

from evals.data._experiment import *  # noqa: F401,F403
from evals.data._experiment import (  # noqa: F401
    LangSmithExperiment,
    build_export_experiment,
    configure_tracing_for_dataset,
    ensure_feedback_config,
    git_sha,
    resolve_model_label,
)
```

- [ ] **Step 4: Replace `evals/runtime/datasets.py` with a shim**

```python
# evals/runtime/datasets.py
"""Deprecated: use `evals.data._samples`. Removed in Task 15."""

from __future__ import annotations

from evals.data._samples import *  # noqa: F401,F403
from evals.data._samples import (  # noqa: F401
    SWE_QA_PRO_REPO_PATH,
    issue_row_to_sample,
    langsmith_dataset,
    load_issue_dataset,
    qa_example_to_agent_sample,
    qa_example_to_sample,
    review_example_to_sample,
)
```

- [ ] **Step 5: Run the entire eval test suite to confirm shims work**

```bash
uv run pytest tests/eval -v
```

Expected: green for every legacy file (`test_datasets.py`, `test_predictions.py`, `test_inspect_helpers.py`, `test_langsmith_experiments.py`).

- [ ] **Step 6: `make check`**

```bash
make check
```

- [ ] **Step 7: Commit**

```bash
git add evals/data/__init__.py evals/runtime/predictions.py evals/runtime/langsmith.py evals/runtime/datasets.py
git commit -m "refactor(evals): wire EvalDataset singletons and collapse runtime modules to shims"
```

---

### Task 12: Rewrite the four task files to use suite singletons

**Files:**
- Modify: `evals/tasks/fix_swe_bench.py`
- Modify: `evals/tasks/test_swt_bench.py`
- Modify: `evals/tasks/chat_swe_qa.py`
- Modify: `evals/tasks/review_martian.py`
- Create: `evals/tasks/_review_overlap_scorer.py`
- Modify: `tests/eval/test_task_wiring.py`

Goal: prove the architecture by collapsing each task body to ~10 lines that just plug a solver (and optionally a scorer) into `<SUITE>.build_task(...)`. Per the prior conversation, **the task body should not contain task-unrelated logic** — the user explicitly called this out for `review_martian.py`.

- [ ] **Step 1: Extract `review_overlap` scorer wiring out of the task**

```python
# evals/tasks/_review_overlap_scorer.py
"""Scorer factory for the Martian-CRB overlap metric.

Lifted out of `review_martian.py` so the task module can stay declarative.
"""

from __future__ import annotations

from typing import cast

from evals.scorers.review_judge import judge_verdict as martian_judge_verdict
from evals.scorers.review_overlap import JudgeFn, review_overlap_f1_scorer


def make_review_overlap_scorer():  # type: ignore[no-untyped-def]
    return review_overlap_f1_scorer(cast(JudgeFn, martian_judge_verdict))
```

- [ ] **Step 2: Rewrite `evals/tasks/review_martian.py`**

```python
# evals/tasks/review_martian.py
"""Martian-style PR review task — PRD §4.1 / §14 E2."""

from __future__ import annotations

from inspect_ai import Task, task

from evals.data import REVIEW
from evals.scorers.review_judge import (
    MARTIAN_JUDGE_MODEL_ID,
    MARTIAN_JUDGE_VERSION,
)
from evals.solvers.review import openbot_review_solver
from evals.tasks._review_overlap_scorer import make_review_overlap_scorer


@task
def review_martian_openbot() -> Task:
    """OpenBot review solver + verbatim Martian-CRB LLM judge."""

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

- [ ] **Step 3: Rewrite `evals/tasks/fix_swe_bench.py`**

```python
# evals/tasks/fix_swe_bench.py
"""SWE-bench Verified — PRD §3.1 (`fix_swe_bench` eval surface)."""

from __future__ import annotations

from inspect_ai import Task, task

from evals.data import FIX
from evals.solvers.fix import openbot_fix_solver


@task
def fix_swe_bench() -> Task:
    """SWE-bench Verified with the OpenBot fix solver."""

    return FIX.build_task(solver=openbot_fix_solver())
```

- [ ] **Step 4: Rewrite `evals/tasks/test_swt_bench.py`**

```python
# evals/tasks/test_swt_bench.py
"""SWT-Bench Verified — PRD §3.1 (`test_swt_bench` eval surface)."""

from __future__ import annotations

from inspect_ai import Task, task

from evals.data import SWT
from evals.solvers.test_generation import openbot_test_generation_solver


@task
def test_swt_bench() -> Task:
    """SWT-Bench Verified with the OpenBot test-generation solver."""

    return SWT.build_task(solver=openbot_test_generation_solver())
```

- [ ] **Step 5: Rewrite `evals/tasks/chat_swe_qa.py`**

```python
# evals/tasks/chat_swe_qa.py
"""SWE-QA-Pro chat task — PRD §3.1 (E1.b)."""

from __future__ import annotations

from inspect_ai import Task, task

from evals.data import CHAT
from evals.scorers.swe_qa_pro import swe_qa_pro_judge_scorer
from evals.solvers.chat import openbot_chat_solver


@task
def chat_swe_qa_pro() -> Task:
    return CHAT.build_task(
        solver=openbot_chat_solver(),
        scorer=swe_qa_pro_judge_scorer(),
    )
```

- [ ] **Step 6: Update `tests/eval/test_task_wiring.py` to monkeypatch `<SUITE>.build_task`**

```python
# tests/eval/test_task_wiring.py — replace any monkeypatching of review_martian
# internals with stubs on the suite singleton.
from evals.data import REVIEW


def test_review_martian_calls_build_task(monkeypatch):  # type: ignore[no-untyped-def]
    captured: dict = {}

    def _stub_build_task(self, *, solver, scorer=None, extra_metadata=None):  # type: ignore[no-untyped-def]
        captured["called"] = True
        captured["scorer"] = scorer
        captured["solver"] = solver
        return "task-sentinel"

    monkeypatch.setattr(type(REVIEW), "build_task", _stub_build_task, raising=False)

    from evals.tasks.review_martian import review_martian_openbot

    assert review_martian_openbot() == "task-sentinel"
    assert captured["called"] is True
    assert captured["scorer"] is not None
```

- [ ] **Step 7: Run + `make check`**

```bash
uv run pytest tests/eval -v
make check
```

Expected: green.

- [ ] **Step 8: Commit**

```bash
git add evals/tasks/ tests/eval/test_task_wiring.py
git commit -m "refactor(evals/tasks): collapse task bodies to suite.build_task()"
```

---

### Task 13: Rewrite the Makefile recipes to call the new CLI

**Files:**
- Modify: `evals/Makefile`
- Modify: `tests/eval/test_makefile.py`

Goal: replace the eight `python -m evals.scripts.build_* / writeback_*` invocations with `python -m evals.data <verb> <suite>` calls. Keep target names (`data-review`, `data-fix-refresh`, `writeback-fix`, `writeback-test`) so callers don't break.

- [ ] **Step 1: Rewrite the data targets**

```make
data: data-review data-chat data-fix data-test ## Publish all eval datasets safely

data-refresh: data-review-refresh data-chat-refresh data-fix-refresh data-test-refresh ## Rebuild all eval datasets

data-review: ## Publish Martian review dataset
	$(DOPPLER) $(PY) python -m evals.data refresh review

data-chat: ## Publish SWE-QA-Pro chat dataset
	$(DOPPLER) $(PY) python -m evals.data refresh chat

data-fix: ## Publish SWE-bench Verified mirror
	$(DOPPLER) $(PY) python -m evals.data refresh fix

data-test: ## Publish SWT-Bench Verified mirror
	$(DOPPLER) $(PY) python -m evals.data refresh swt

data-review-refresh: data-review
data-chat-refresh: data-chat
data-fix-refresh: data-fix
data-test-refresh: data-test
```

(`refresh` is idempotent under the new CLI: it re-collects + re-publishes regardless of whether the dataset existed. The `--force` flag becomes redundant — drop it.)

- [ ] **Step 2: Rewrite the writeback targets**

The existing recipes auto-discover the latest harness report and predictions file. We keep that bash logic and only swap the final `python -m` invocation. (The two recipes are otherwise twins, so this is the diff: `evals.scripts.writeback_{swe,swt}_grades` → `evals.data writeback {fix,swt}` with renamed flags.)

```make
writeback-test: ## Writeback SWT harness verdicts → swt_bench_pass_at_1
	@# (… keep the existing shell preamble that resolves $$GRADE_REPORT and $$EXP_NAME …)
	cd .. && $(DOPPLER) $(PY) python -m evals.data writeback swt \
		--report "$$GRADE_REPORT" \
		--experiment "$$EXP_NAME" \
		$(if $(DRY_RUN),--dry-run)

writeback-fix: ## Writeback SWE harness verdicts → swe_bench_pass_at_1
	@# (… keep the existing shell preamble that resolves $$GRADE_FIX_REPORT and $$FIX_EXP_NAME …)
	cd .. && $(DOPPLER) $(PY) python -m evals.data writeback fix \
		--report "$$GRADE_FIX_REPORT" \
		--experiment "$$FIX_EXP_NAME" \
		$(if $(DRY_RUN),--dry-run)
```

Concretely: open `evals/Makefile`, locate `python -m evals.scripts.writeback_swt_grades` (~line 280) and `python -m evals.scripts.writeback_swe_grades` (~line 425), and replace each with the corresponding `python -m evals.data writeback <suite>` call above. The flag rename is `--evaluation-result` → `--report` and `--experiment-name` → `--experiment` (matches the CLI argparse from Task 10).

- [ ] **Step 3: Update `tests/eval/test_makefile.py`**

```python
# tests/eval/test_makefile.py — adjust the assertions
def test_data_recipe_uses_new_cli() -> None:
    text = (Path(__file__).resolve().parents[2] / "evals" / "Makefile").read_text()
    assert "python -m evals.data refresh review" in text
    assert "python -m evals.data refresh chat" in text
    assert "python -m evals.data refresh fix" in text
    assert "python -m evals.data refresh swt" in text


def test_writeback_recipes_use_new_cli() -> None:
    text = (Path(__file__).resolve().parents[2] / "evals" / "Makefile").read_text()
    assert "python -m evals.data writeback fix" in text
    assert "python -m evals.data writeback swt" in text
```

- [ ] **Step 4: Run + `make check`**

```bash
uv run pytest tests/eval/test_makefile.py -v
make check
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add evals/Makefile tests/eval/test_makefile.py
git commit -m "refactor(evals/Makefile): point data/writeback recipes at unified CLI"
```

---

### Task 14: Delete the legacy `evals/scripts/` modules

**Files:** delete six scripts.

Goal: now that the CLI is the canonical entry point, remove the scripts. **Precondition:** there must be zero callers — verify with `git grep`.

- [ ] **Step 1: Confirm no callers remain**

```bash
git grep -nE "evals\.scripts\.(build_|writeback_)" || echo "OK: zero callers"
```

Expected: `OK: zero callers`. If anything appears (e.g., docstrings, comments), update those references first to point at `evals.data`.

- [ ] **Step 2: Delete the scripts**

```bash
git rm evals/scripts/build_review_martian_dataset.py
git rm evals/scripts/build_chat_swe_qa_pro_dataset.py
git rm evals/scripts/build_swe_bench_verified_dataset.py
git rm evals/scripts/build_swt_bench_verified_dataset.py
git rm evals/scripts/writeback_swe_grades.py
git rm evals/scripts/writeback_swt_grades.py
```

- [ ] **Step 3: Move script-internal helpers into the suite modules**

The transitional `from evals.scripts.build_*` imports inside `review.py` / `chat.py` (Tasks 6, 7) need to be inlined now. Open each suite file and replace the import with the actual code (e.g., the async `fetch_pull_requests` body moves into `ReviewDataset._collect_async`).

- [ ] **Step 4: Run + `make check`**

```bash
uv run pytest tests/eval -v
make check
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add -A evals
git commit -m "refactor(evals): delete legacy build_*/writeback_* scripts"
```

---

### Task 15: Delete the runtime shim modules

**Files:** delete three modules.

Goal: now that no caller imports `evals.runtime.{predictions,langsmith,datasets}`, remove the shims.

- [ ] **Step 1: Confirm zero importers**

```bash
git grep -nE "evals\.runtime\.(predictions|langsmith|datasets)" || echo "OK: shim is dead"
```

Expected: `OK: shim is dead`. If lines remain (e.g., test files), rewrite them to import from `evals.data._predictions` / `_experiment` / `_samples` first.

- [ ] **Step 2: Delete the shims**

```bash
git rm evals/runtime/predictions.py
git rm evals/runtime/langsmith.py
git rm evals/runtime/datasets.py
```

- [ ] **Step 3: Run + `make check`**

```bash
uv run pytest tests/eval -v
make check
```

Expected: green.

- [ ] **Step 4: Commit**

```bash
git add -A evals
git commit -m "refactor(evals/runtime): drop predictions/langsmith/datasets shims"
```

---

### Task 16: Replace the legacy test files

**Files:** delete four test files; the parametrized `test_data_unified.py` / `test_data_predictions.py` / `test_data_writeback.py` / `test_data_cli.py` cover everything they did.

Goal: the unified test files replace the per-module ones. We delete legacy tests last so each prior task can verify against both old and new test files.

- [ ] **Step 1: Audit coverage parity**

For each legacy file, list the test functions and check the corresponding assertion exists in the new file. (The four `test_data_*.py` files added in Tasks 1, 4, 5, 9, 10 collectively cover every case.)

- [ ] **Step 2: Delete the legacy tests**

```bash
git rm tests/eval/test_datasets.py
git rm tests/eval/test_predictions.py
git rm tests/eval/test_inspect_helpers.py
git rm tests/eval/test_langsmith_experiments.py
```

- [ ] **Step 3: Run the full eval test suite**

```bash
uv run pytest tests/eval -v
```

Expected: green.

- [ ] **Step 4: `make check`**

```bash
make check
```

Expected: green.

- [ ] **Step 5: Commit + archive the plan/spec**

```bash
git add -A tests/eval
git commit -m "test(evals): drop legacy per-module suites in favour of test_data_*"

# Archival rule from CLAUDE.md: completed plan/spec → docs/_archive/
mkdir -p docs/_archive/superpowers
mv docs/superpowers/plans/2026-05-24-evals-data-unification.md docs/_archive/superpowers/
mv docs/superpowers/specs/2026-05-24-evals-data-unification-design.md docs/_archive/superpowers/
git add docs/
git commit -m "docs: archive eval-data-unification plan and spec"
```

---

## Self-Review Checklist (the engineer should verify before declaring the plan complete)

- [ ] `git grep -n "evals.runtime.predictions"` returns nothing.
- [ ] `git grep -n "evals.runtime.langsmith"` returns nothing.
- [ ] `git grep -n "evals.runtime.datasets"` returns nothing.
- [ ] `git grep -n "from evals.scripts.build_"` returns nothing.
- [ ] `git grep -n "from evals.scripts.writeback_"` returns nothing.
- [ ] `make check` is green on the final commit.
- [ ] The four task files (`fix_swe_bench.py`, `test_swt_bench.py`, `chat_swe_qa.py`, `review_martian.py`) each contain a single `@task` body that delegates to `<SUITE>.build_task(...)` and no LangSmith / dataset / experiment imports.
- [ ] `python -m evals.data refresh fix` produces a non-zero exit only on real failure (smoke-tested manually with `--dry-run` scaffolding if available).

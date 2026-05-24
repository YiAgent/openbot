# Eval Data Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse `evals/scripts/build_*` (4 scripts), `evals/scripts/writeback_*` (2 scripts), and `evals/runtime/{datasets,predictions,langsmith}.py` (3 modules) into one `evals/data/` package. Each suite is a single `EvalDataset` singleton owning its full data lifecycle. `EvalDataset.build_task()` returns a **pure `inspect_ai.Task`** — no LangSmith imports, no experiment wiring. All LangSmith observability is owned by the existing `langsmith_hook.py` via the metadata-key contract.

**Core architectural rule:** task files are declarative — `@task def f() -> Task: return SUITE.build_task(solver=...)`. All LangSmith lifecycle (experiment provision, Run creation, Feedback) lives in the `@hooks` class, not in `build_task()` or scorers.

**Spec:** `docs/superpowers/specs/2026-05-24-evals-data-unification-design.md`

**Tech Stack:** Python 3.12 · pydantic / pydantic-settings · langsmith client · datasets (HuggingFace) · inspect_ai · pytest · uv

---

## File Structure

### New (created in this plan)

| Path | Responsibility |
|------|----------------|
| `evals/data/__init__.py` | Re-exports `EvalDataset`, `REVIEW`/`CHAT`/`FIX`/`SWT` singletons, `DATASETS` map |
| `evals/data/__main__.py` | CLI router: `python -m evals.data <verb> <suite>` |
| `evals/data/_base.py` | `EvalDataset` ABC, `CollectedExample`, `PublishResult`, `WritebackSummary`, `sha256_examples`, `chunked_publish` |
| `evals/data/_predictions.py` | Schemas + `_AppendWriter` + `prediction_exporter` scorer (moved from `runtime/predictions.py`) |
| `evals/data/_samples.py` | LangSmith→Inspect converters + `langsmith_dataset` + `load_issue_dataset` (moved from `runtime/datasets.py`) |
| `evals/data/_utils.py` | `git_sha`, `resolve_model_label`, `ensure_feedback_config`, `reset_feedback_cache_for_tests` (subset of `runtime/langsmith.py` — experiment lifecycle stripped) |
| `evals/data/_writeback.py` | Generic `run_writeback()` driver (dedupes `writeback_swe_grades.py` ↔ `writeback_swt_grades.py`) |
| `evals/data/review.py` | `ReviewDataset` |
| `evals/data/chat.py` | `ChatDataset` |
| `evals/data/fix.py` | `FixDataset` — pure `build_task()` + `classify()` for writeback |
| `evals/data/swt.py` | `SwtDataset` — pure `build_task()` + `classify()` for writeback |
| `evals/tasks/_review_overlap_scorer.py` | Extracted scorer factory (lifts scorer wiring out of `review_martian.py`) |
| `tests/eval/test_data_unified.py` | Parametrized [REVIEW, CHAT, FIX, SWT] lifecycle + pure-Task assertion |
| `tests/eval/test_data_predictions.py` | Schema + exporter tests |
| `tests/eval/test_data_writeback.py` | Parametrized [FIX, SWT] writeback tests |
| `tests/eval/test_data_cli.py` | CLI routing tests |

### Modified

| Path | Change |
|------|--------|
| `evals/runtime/langsmith_hook.py` | Task 12: extend `on_task_end` to push aggregate metrics |
| `evals/runtime/datasets.py` | Task 11: thin re-export shim → Task 16: deleted |
| `evals/runtime/predictions.py` | Task 11: thin re-export shim → Task 16: deleted |
| `evals/runtime/langsmith.py` | Task 11: thin re-export shim (only 4 utils) → Task 16: deleted |
| `evals/runtime/config.py` | **Unchanged** |
| `evals/tasks/fix_swe_bench.py` | Task 13: pure 5-line `@task` body |
| `evals/tasks/test_swt_bench.py` | Task 13: pure 5-line `@task` body |
| `evals/tasks/chat_swe_qa.py` | Task 13: pure 8-line `@task` body |
| `evals/tasks/review_martian.py` | Task 13: pure 12-line `@task` body |
| `evals/Makefile` | Task 14: rewrite `data-*` and `writeback-*` targets |
| `tests/eval/test_task_wiring.py` | Task 13: monkeypatch suite singleton's `build_task` |
| `tests/eval/test_langsmith_hook.py` (existing `test_langsmith_experiments.py`) | Task 12: extend with `on_task_end` aggregate metric tests |

### Deleted (steps 15-17)

| Path | Step |
|------|------|
| `evals/scripts/build_review_martian_dataset.py` | 15 |
| `evals/scripts/build_chat_swe_qa_pro_dataset.py` | 15 |
| `evals/scripts/build_swe_bench_verified_dataset.py` | 15 |
| `evals/scripts/build_swt_bench_verified_dataset.py` | 15 |
| `evals/scripts/writeback_swe_grades.py` | 15 |
| `evals/scripts/writeback_swt_grades.py` | 15 |
| `evals/runtime/datasets.py` | 16 |
| `evals/runtime/predictions.py` | 16 |
| `evals/runtime/langsmith.py` | 16 |
| `tests/eval/test_datasets.py` | 17 |
| `tests/eval/test_predictions.py` | 17 |
| `tests/eval/test_inspect_helpers.py` | 17 |
| `tests/eval/test_langsmith_experiments.py` | 17 |

---

## Conventions

- All commits run `make check` (fmt-check + lint + test) green before committing.
- Use `uv run pytest <path>` not `pip install pytest`.
- Use `git mv` when moving a file to preserve history; otherwise `cp` followed by `git add`.
- New modules go under `evals/data/`. Module-private names use underscore prefix (`_base.py`, `_utils.py`).
- Type annotations on every public function. `from __future__ import annotations` at the top of every new module.
- **No langsmith imports inside `build_task()` or any task file.** Violation = pre-commit hook failure.

---

## Tasks

### Task 1: Scaffold `evals/data/` package — copy `_predictions.py` and `_samples.py`

**Files:**
- Create: `evals/data/__init__.py` (empty package marker)
- Create: `evals/data/_predictions.py` (copy of `runtime/predictions.py`)
- Create: `evals/data/_samples.py` (copy of `runtime/datasets.py`)
- Test: `tests/eval/test_data_predictions.py` (new)

Goal: establish the package, relocate the two pure data modules, prove they expose the same public API.

- [ ] **Step 1: Create package and copies**

```bash
mkdir -p evals/data
# evals/data/__init__.py: just the docstring + "singletons wired in Task 11"
cp evals/runtime/predictions.py evals/data/_predictions.py
cp evals/runtime/datasets.py    evals/data/_samples.py
```

- [ ] **Step 2: Write smoke tests**

```python
# tests/eval/test_data_predictions.py
from __future__ import annotations
import pytest
from evals.data import _predictions, _samples

PRED_NAMES = [
    "SweBenchPrediction", "SwtBenchPrediction", "SweQaProAnswer", "SweQaProCitation",
    "empty_swe_prediction", "empty_swt_prediction", "predictions_path", "prediction_exporter",
]
SAMPLE_NAMES = [
    "review_example_to_sample", "qa_example_to_sample", "qa_example_to_agent_sample",
    "langsmith_dataset", "load_issue_dataset", "issue_row_to_sample",
]

@pytest.mark.parametrize("name", PRED_NAMES)
def test_predictions_api(name: str) -> None:
    assert hasattr(_predictions, name)

@pytest.mark.parametrize("name", SAMPLE_NAMES)
def test_samples_api(name: str) -> None:
    assert hasattr(_samples, name)
```

- [ ] **Step 3: Run + `make check`**

```bash
uv run pytest tests/eval/test_data_predictions.py tests/eval/test_predictions.py tests/eval/test_datasets.py -v
make check
```

- [ ] **Step 4: Commit**

```bash
git add evals/data/ tests/eval/test_data_predictions.py
git commit -m "feat(evals/data): scaffold package, copy predictions + samples modules"
```

---

### Task 2: Create `evals/data/_utils.py` — three helpers only

**Files:**
- Create: `evals/data/_utils.py`
- Test: append to `tests/eval/test_data_predictions.py`

Goal: extract **only** `git_sha`, `resolve_model_label`, `ensure_feedback_config`, `reset_feedback_cache_for_tests` from `runtime/langsmith.py`. Do **not** copy `LangSmithExperiment`, `configure_tracing_for_dataset`, or `build_export_experiment` — those are eliminated by this refactor.

- [ ] **Step 1: Write `_utils.py`**

```python
# evals/data/_utils.py
"""Minimal utilities used by suite build_task() methods and the langsmith hook.

Intentionally small: experiment lifecycle (LangSmithExperiment, configure_tracing,
build_export_experiment) is NOT here — it belongs entirely to langsmith_hook.py.
"""
from __future__ import annotations
import logging, os, subprocess
from typing import Any

logger = logging.getLogger(__name__)

_RECONCILED_FEEDBACK_KEYS: set[str] = set()


def git_sha() -> str:
    """Best-effort current git SHA for run metadata."""
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def resolve_model_label() -> str:
    """Best-effort model label for task metadata (not a routing decision)."""
    try:
        from openbot.core.settings import Settings
        return Settings().model or "openbot"
    except Exception:
        return "openbot"


def ensure_feedback_config(client: Any, key: str, config: dict[str, Any]) -> None:
    """Idempotently reconcile a LangSmith feedback key's display config."""
    if key in _RECONCILED_FEEDBACK_KEYS:
        return
    try:
        try:
            client.create_feedback_config(feedback_key=key, feedback_config=config)
        except Exception:
            client.update_feedback_config(feedback_key=key, feedback_config=config)
        _RECONCILED_FEEDBACK_KEYS.add(key)
    except Exception as exc:
        logger.warning("langsmith: failed to reconcile feedback config for %r: %s", key, exc)


def reset_feedback_cache_for_tests() -> None:
    """Drop the per-process feedback reconciliation cache (test fixture only)."""
    _RECONCILED_FEEDBACK_KEYS.clear()
```

- [ ] **Step 2: Add smoke test**

```python
# tests/eval/test_data_predictions.py — append
from evals.data import _utils

def test_utils_exports() -> None:
    for name in ("git_sha", "resolve_model_label", "ensure_feedback_config",
                 "reset_feedback_cache_for_tests"):
        assert hasattr(_utils, name), f"_utils missing {name!r}"
    # Confirm eliminated names are NOT present
    for name in ("LangSmithExperiment", "configure_tracing_for_dataset", "build_export_experiment"):
        assert not hasattr(_utils, name), f"_utils must NOT contain {name!r}"
```

- [ ] **Step 3: Run + `make check`**

```bash
uv run pytest tests/eval/test_data_predictions.py -v
make check
```

- [ ] **Step 4: Commit**

```bash
git add evals/data/_utils.py tests/eval/test_data_predictions.py
git commit -m "feat(evals/data): add _utils.py (git_sha, resolve_model_label, ensure_feedback_config)"
```

---

### Task 3: Generic writeback driver `evals/data/_writeback.py`

**Files:**
- Create: `evals/data/_writeback.py`
- Test: `tests/eval/test_data_writeback.py` (new, skipped until Task 9)

- [ ] **Step 1: Write `_writeback.py`**

```python
# evals/data/_writeback.py
"""Generic offline-grade writeback driver.

Replaces writeback_swe_grades.py / writeback_swt_grades.py.
Suite supplies `feedback_key: str` and static `classify(report)` generator.
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


def run_writeback(
    *,
    suite: _WritebackSuite,
    report_path: str,
    experiment_name: str,
    dry_run: bool = False,
    client: Any | None = None,
) -> WritebackSummary:
    if client is None:
        from langsmith import Client
        client = Client()

    report = json.loads(Path(report_path).read_text(encoding="utf-8"))

    # Index runs by instance_id (stored in extra.metadata.instance_id by the hook)
    runs_by_id: dict[str, list[Any]] = {}
    for run in client.list_runs(project_name=experiment_name, is_root=True):
        iid = (run.extra or {}).get("metadata", {}).get("instance_id")
        if iid:
            runs_by_id.setdefault(str(iid), []).append(run)

    written = matched = unmatched = 0
    for instance_id, score, comment in suite.classify(report):
        instance_runs = runs_by_id.get(instance_id, [])
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

- [ ] **Step 2: Write test (initially skipped)**

```python
# tests/eval/test_data_writeback.py
"""Enabled in Task 9 once FIX/SWT singletons land."""
from __future__ import annotations
import pytest

pytestmark = pytest.mark.skip(reason="enabled in Task 9")
# Tests are pre-written; remove the skip marker in Task 9 Step 3.

from types import SimpleNamespace
from typing import Any
from evals.data._base import WritebackSummary
from evals.data._writeback import run_writeback


@pytest.fixture(params=["fix", "swt"])
def graded_suite(request):
    from evals.data import DATASETS
    return DATASETS[request.param]


def test_writeback_creates_feedback(graded_suite, tmp_path) -> None:
    report = tmp_path / "r.json"
    report.write_text('{"resolved":["a__b-1"],"unresolved":[],"error":[]}')
    feedbacks: list[dict[str, Any]] = []

    class _FakeClient:
        def list_runs(self, **kw):
            return iter([SimpleNamespace(
                id="run-1",
                extra={"metadata": {"instance_id": "a__b-1"}},
            )])
        def create_feedback(self, **kw):
            feedbacks.append(kw)

    summary = run_writeback(
        suite=graded_suite, report_path=str(report),
        experiment_name="exp", dry_run=False, client=_FakeClient(),
    )
    assert summary.feedback_written == 1
    assert feedbacks[0]["score"] == 1.0
    assert feedbacks[0]["key"] == graded_suite.feedback_key


def test_writeback_dry_run(tmp_path) -> None:
    from evals.data import FIX
    report = tmp_path / "r.json"
    report.write_text('{"resolved":["x"],"unresolved":[],"error":[]}')

    class _FakeClient:
        def list_runs(self, **kw):
            return iter([SimpleNamespace(id="r", extra={"metadata": {"instance_id": "x"}})])
        def create_feedback(self, **kw):
            raise AssertionError("dry_run must not write")

    summary = run_writeback(
        suite=FIX, report_path=str(report),
        experiment_name="e", dry_run=True, client=_FakeClient(),
    )
    assert summary.dry_run is True
    assert summary.feedback_written == 0


@pytest.mark.parametrize("suite_name", ["review", "chat"])
def test_writeback_raises_not_implemented(suite_name: str) -> None:
    from evals.data import DATASETS
    suite = DATASETS[suite_name]
    with pytest.raises(NotImplementedError):
        suite.writeback_grades(report_path="x", experiment_name="e")
```

- [ ] **Step 3: Run + `make check`**

```bash
uv run pytest tests/eval/test_data_writeback.py -v   # skipped — OK
make check
```

- [ ] **Step 4: Commit**

```bash
git add evals/data/_writeback.py tests/eval/test_data_writeback.py
git commit -m "feat(evals/data): add generic writeback driver"
```

---

### Task 4: `EvalDataset` ABC — `evals/data/_base.py`

**Files:**
- Create: `evals/data/_base.py`
- Test: `tests/eval/test_data_unified.py` (new, red first)

Goal: introduce the ABC. `build_task()` is abstract — each suite overrides it with a pure `Task(...)` return. No LangSmith in the base.

- [ ] **Step 1: Write failing tests**

```python
# tests/eval/test_data_unified.py
from __future__ import annotations
from typing import Any, ClassVar
import pytest
from inspect_ai.dataset import MemoryDataset, Sample
from evals.data._base import CollectedExample, EvalDataset, PublishResult, sha256_examples


class _FakeSuite(EvalDataset):
    suite: ClassVar[str] = "fake"
    dataset_version: ClassVar[str] = "fake_v1"
    instance_id_field: ClassVar[str] = "id"

    def collect(self) -> list[CollectedExample]:
        return [
            CollectedExample(inputs={"id": "a"}, outputs=None, metadata={}),
            CollectedExample(inputs={"id": "b"}, outputs=None, metadata={}),
        ]
    def example_to_sample(self, example: Any) -> Sample:
        return Sample(id=example.inputs["id"], input="")
    def build_task(self, *, solver, scorer=None, extra_metadata=None):
        raise NotImplementedError


def test_sha256_stable_under_key_order() -> None:
    a = [CollectedExample(inputs={"id": "1", "x": 1}, outputs=None, metadata={})]
    b = [CollectedExample(inputs={"x": 1, "id": "1"}, outputs=None, metadata={})]
    assert sha256_examples(a) == sha256_examples(b)


def test_publish_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _FakeSuite()
    seen: list[int] = []

    class _FakeClient:
        def read_dataset(self, dataset_name):
            raise LookupError(dataset_name)
        def create_dataset(self, dataset_name, description=""):
            return type("D", (), {"id": "00000000-0000-0000-0000-000000000000"})()
        def create_examples(self, *, inputs, outputs, metadata, dataset_id):
            seen.append(len(inputs))

    examples = [CollectedExample(inputs={"id": str(i)}, outputs=None, metadata={}) for i in range(250)]
    result = suite.publish(examples, client=_FakeClient(), chunk_size=100)
    assert isinstance(result, PublishResult)
    assert seen == [100, 100, 50]
    assert result.example_count == 250


def test_writeback_default_raises() -> None:
    suite = _FakeSuite()
    with pytest.raises(NotImplementedError):
        suite.writeback_grades(report_path="x", experiment_name="e")
```

- [ ] **Step 2: Run (expect ImportError)**

```bash
uv run pytest tests/eval/test_data_unified.py -v
```

- [ ] **Step 3: Write `_base.py`**

```python
# evals/data/_base.py
"""EvalDataset ABC and shared data types.

build_task() is abstract and MUST return a pure inspect_ai.Task.
No LangSmith imports belong here — all observability is owned by langsmith_hook.py.
"""
from __future__ import annotations
import hashlib, json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Iterable, TypedDict
from inspect_ai.dataset import MemoryDataset
from inspect_ai.scorer import Scorer


class CollectedExample(TypedDict):
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


def sha256_examples(examples: Iterable[CollectedExample]) -> str:
    digest = hashlib.sha256()
    for ex in examples:
        digest.update(json.dumps(dict(ex), sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


class EvalDataset(ABC):
    suite: ClassVar[str]
    dataset_version: ClassVar[str]
    instance_id_field: ClassVar[str] = "id"
    description: ClassVar[str] = ""

    @abstractmethod
    def collect(self) -> list[CollectedExample]: ...

    @abstractmethod
    def example_to_sample(self, example: Any) -> Any: ...

    @abstractmethod
    def build_task(self, *, solver: Any, scorer: Scorer | None = None,
                   extra_metadata: dict[str, Any] | None = None) -> Any: ...

    def publish(self, examples: list[CollectedExample], *,
                client: Any | None = None, chunk_size: int = 100) -> PublishResult:
        if client is None:
            from langsmith import Client
            client = Client()
        try:
            ds = client.read_dataset(dataset_name=self.dataset_version)
        except LookupError:
            ds = client.create_dataset(dataset_name=self.dataset_version,
                                        description=self.description)
        for start in range(0, len(examples), chunk_size):
            batch = examples[start:start + chunk_size]
            client.create_examples(
                inputs=[e["inputs"] for e in batch],
                outputs=[e.get("outputs") for e in batch],
                metadata=[e.get("metadata") or {} for e in batch],
                dataset_id=str(ds.id),
            )
        return PublishResult(
            dataset_id=str(ds.id),
            dataset_name=self.dataset_version,
            example_count=len(examples),
            sha256=sha256_examples(examples),
        )

    def load_for_inspect(self) -> MemoryDataset:
        from evals.data import _samples
        return _samples.langsmith_dataset(
            self.dataset_version, converter=self.example_to_sample
        )

    def writeback_grades(self, *, report_path: str, experiment_name: str,
                         dry_run: bool = False, client: Any | None = None) -> WritebackSummary:
        raise NotImplementedError(
            f"{type(self).__name__} does not support offline grade writeback."
        )
```

- [ ] **Step 4: Run + `make check`**

```bash
uv run pytest tests/eval/test_data_unified.py -v
make check
```

- [ ] **Step 5: Commit**

```bash
git add evals/data/_base.py tests/eval/test_data_unified.py
git commit -m "feat(evals/data): introduce EvalDataset ABC with publish/load/sha256"
```

---

### Task 5: `ReviewDataset`

**Files:** `evals/data/review.py` + append to `test_data_unified.py`

`build_task()` returns a pure `Task`. No LangSmith imports.

- [ ] **Step 1: Implement**

```python
# evals/data/review.py
from __future__ import annotations
import asyncio
from typing import Any, ClassVar
from inspect_ai import Task
from inspect_ai.scorer import Scorer
from evals.data._base import CollectedExample, EvalDataset
from evals.data._samples import review_example_to_sample
from evals.data._utils import git_sha, resolve_model_label


class ReviewDataset(EvalDataset):
    suite: ClassVar[str] = "review"
    dataset_version: ClassVar[str] = "martian_2026w20"
    instance_id_field: ClassVar[str] = "id"
    description: ClassVar[str] = "Martian-CRB PR review prompts"

    def collect(self) -> list[CollectedExample]:
        return asyncio.run(self._collect_async())

    async def _collect_async(self) -> list[CollectedExample]:
        # Transitional: inlined in Task 15 when the build script is deleted.
        from evals.scripts.build_review_martian_dataset import _collect_and_build_examples
        return await _collect_and_build_examples()

    def example_to_sample(self, example: Any) -> Any:
        return review_example_to_sample(example)

    def build_task(self, *, solver: Any, scorer: Scorer | None = None,
                   extra_metadata: dict[str, Any] | None = None) -> Task:
        if scorer is None:
            raise ValueError("ReviewDataset.build_task requires an explicit scorer=")
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

- [ ] **Step 2: Add test + run + commit**

```python
# tests/eval/test_data_unified.py — append
from evals.data.review import ReviewDataset

def test_review_attrs() -> None:
    assert ReviewDataset.suite == "review"
    assert ReviewDataset.dataset_version == "martian_2026w20"

def test_review_build_task_raises_without_scorer(monkeypatch) -> None:
    from unittest.mock import MagicMock
    r = ReviewDataset()
    monkeypatch.setattr(r, "load_for_inspect", MagicMock(return_value=MagicMock()))
    import pytest
    with pytest.raises(ValueError, match="scorer"):
        r.build_task(solver=MagicMock())

def test_review_build_task_has_no_langsmith_imports(monkeypatch) -> None:
    """Task metadata must not contain langsmith experiment objects."""
    from unittest.mock import MagicMock
    r = ReviewDataset()
    monkeypatch.setattr(r, "load_for_inspect", MagicMock(return_value=MagicMock()))
    task = r.build_task(solver=MagicMock(), scorer=MagicMock())
    for v in (task.metadata or {}).values():
        assert not hasattr(v, "create_run"), "LangSmith object leaked into Task metadata"
```

```bash
uv run pytest tests/eval/test_data_unified.py -v
make check
git add evals/data/review.py tests/eval/test_data_unified.py
git commit -m "feat(evals/data): add ReviewDataset with pure build_task"
```

---

### Task 6: `ChatDataset`

Mirror of Task 5 for `chat_swe_qa_pro_v1`.

- [ ] **Step 1–4:** Same pattern as Task 5.

```python
# evals/data/chat.py
from __future__ import annotations
from typing import Any, ClassVar
from inspect_ai import Task
from inspect_ai.scorer import Scorer
from evals.data._base import CollectedExample, EvalDataset
from evals.data._samples import qa_example_to_agent_sample
from evals.data._utils import git_sha, resolve_model_label


class ChatDataset(EvalDataset):
    suite: ClassVar[str] = "chat"
    dataset_version: ClassVar[str] = "chat_swe_qa_pro_v1"
    hf_dataset: ClassVar[str] = "TIGER-Lab/SWE-QA-Pro-Bench"
    instance_id_field: ClassVar[str] = "id"
    description: ClassVar[str] = "SWE-QA-Pro grounded chat tasks"

    def collect(self) -> list[CollectedExample]:
        from evals.scripts.build_chat_swe_qa_pro_dataset import _collect_and_build_examples
        return _collect_and_build_examples()

    def example_to_sample(self, example: Any) -> Any:
        return qa_example_to_agent_sample(example)

    def build_task(self, *, solver: Any, scorer: Scorer | None = None,
                   extra_metadata: dict[str, Any] | None = None) -> Task:
        if scorer is None:
            raise ValueError("ChatDataset.build_task requires an explicit scorer=")
        from evals.runtime.config import get_eval_config
        catalog = get_eval_config().catalog
        return Task(
            dataset=self.load_for_inspect(),
            solver=solver,
            scorer=scorer,
            metadata={
                "dataset_version": self.dataset_version,
                "dataset_source": catalog.chat.dataset_source,
                "solver_family": catalog.solver_family_baseline,
                "model": resolve_model_label(),
                "git_sha": git_sha(),
                "instance_id_field": self.instance_id_field,
                **(extra_metadata or {}),
            },
        )
```

```bash
git add evals/data/chat.py tests/eval/test_data_unified.py
git commit -m "feat(evals/data): add ChatDataset with pure build_task"
```

---

### Task 7: `FixDataset`

**Files:** `evals/data/fix.py` + append tests

`build_task()` uses `prediction_exporter` scorer (inspect-native, no LangSmith). The hook reads `metadata["dataset_version"]` and handles experiment anchoring.

- [ ] **Step 1: Implement**

```python
# evals/data/fix.py
from __future__ import annotations
from typing import Any, ClassVar, Iterable
from inspect_ai import Task
from inspect_ai.scorer import Scorer
from evals.data._base import CollectedExample, EvalDataset, WritebackSummary
from evals.data._predictions import SweBenchPrediction, prediction_exporter
from evals.data._samples import issue_row_to_sample
from evals.data._utils import git_sha, resolve_model_label
from evals.data._writeback import run_writeback


class FixDataset(EvalDataset):
    suite: ClassVar[str] = "fix"
    dataset_version: ClassVar[str] = "fix_swe_bench_verified"
    hf_dataset: ClassVar[str] = "princeton-nlp/SWE-bench_Verified"
    instance_id_field: ClassVar[str] = "instance_id"
    description: ClassVar[str] = "SWE-bench Verified fix instances"
    feedback_key: ClassVar[str] = "swe_bench_pass_at_1"

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
                    "PASS_TO_PASS": row.get("PASS_TO_PASS", []),
                    "test_patch": row.get("test_patch", ""),
                },
                outputs={"patch": row.get("patch", "")},
                metadata={"hf_dataset": self.hf_dataset},
            )
            for row in rows
        ]

    def example_to_sample(self, example: Any) -> Any:
        from evals.data._samples import _coerce_attr  # type: ignore[attr-defined]
        return issue_row_to_sample(_coerce_attr(example, "inputs"))

    @staticmethod
    def classify(report: dict[str, Any]) -> Iterable[tuple[str, float, str]]:
        for iid in report.get("resolved", []):
            yield (iid, 1.0, "swe-bench resolved")
        for iid in report.get("unresolved", []):
            yield (iid, 0.0, "swe-bench unresolved")
        for iid in report.get("error", []):
            yield (iid, 0.0, "swe-bench error")

    def writeback_grades(self, *, report_path: str, experiment_name: str,
                         dry_run: bool = False, client: Any | None = None) -> WritebackSummary:
        return run_writeback(suite=self, report_path=report_path,
                             experiment_name=experiment_name, dry_run=dry_run, client=client)

    def build_task(self, *, solver: Any, scorer: Scorer | None = None,
                   extra_metadata: dict[str, Any] | None = None) -> Task:
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
                "dataset_source": f"huggingface:{self.hf_dataset}",
                "solver_family": catalog.solver_family_baseline,
                "model": resolve_model_label(),
                "git_sha": git_sha(),
                "instance_id_field": self.instance_id_field,
                **(extra_metadata or {}),
            },
        )
```

- [ ] **Step 2: Add tests**

```python
# tests/eval/test_data_unified.py — append
from evals.data.fix import FixDataset

def test_fix_attrs() -> None:
    assert FixDataset.suite == "fix"
    assert FixDataset.feedback_key == "swe_bench_pass_at_1"

def test_fix_classify() -> None:
    report = {"resolved": ["a"], "unresolved": ["b"], "error": ["c"]}
    scores = {iid: score for iid, score, _ in FixDataset.classify(report)}
    assert scores == {"a": 1.0, "b": 0.0, "c": 0.0}

def test_fix_build_task_no_langsmith(monkeypatch) -> None:
    from unittest.mock import MagicMock
    f = FixDataset()
    monkeypatch.setattr(f, "load_for_inspect", MagicMock(return_value=MagicMock()))
    task = f.build_task(solver=MagicMock())
    for v in (task.metadata or {}).values():
        assert not hasattr(v, "create_run"), "LangSmith object in Task metadata"
```

- [ ] **Step 3: Run + `make check` + commit**

```bash
uv run pytest tests/eval/test_data_unified.py -v && make check
git add evals/data/fix.py tests/eval/test_data_unified.py
git commit -m "feat(evals/data): add FixDataset with pure build_task + writeback"
```

---

### Task 8: `SwtDataset`

Identical to Task 7, substituting `SwtBenchPrediction`, `swt_bench_pass_at_1`, SWT HF dataset.

- [ ] **Step 1–3:** same pattern. Key differences:
  - `suite = "swt"`, `dataset_version = "test_swt_bench_verified"`
  - `hf_dataset = "eth-sri/SWT-bench_Verified_bm25_27k_zsb"`
  - `feedback_key = "swt_bench_pass_at_1"`
  - scorer uses `SwtBenchPrediction` + `catalog.swt_export_feedback_key`

- [ ] **Step 4: Un-skip `test_data_writeback.py`**

```bash
# Remove the pytestmark skip line from tests/eval/test_data_writeback.py
```

- [ ] **Step 5: Run + `make check` + commit**

```bash
uv run pytest tests/eval/test_data_unified.py tests/eval/test_data_writeback.py -v && make check
git add evals/data/swt.py tests/eval/test_data_unified.py tests/eval/test_data_writeback.py
git commit -m "feat(evals/data): add SwtDataset + enable writeback tests"
```

---

### Task 9: CLI `evals/data/__main__.py`

**Files:** `evals/data/__main__.py` + `tests/eval/test_data_cli.py`

```python
# evals/data/__main__.py
from __future__ import annotations
import argparse, json, sys
from evals.data._base import EvalDataset


def _resolve(name: str) -> EvalDataset:
    from evals.data import DATASETS
    try:
        return DATASETS[name]
    except KeyError:
        raise SystemExit(f"unknown suite {name!r}; choices: {sorted(DATASETS)}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m evals.data")
    sub = p.add_subparsers(dest="cmd", required=True)

    for verb in ("collect", "refresh"):
        s = sub.add_parser(verb)
        s.add_argument("suite")

    pub = sub.add_parser("publish")
    pub.add_argument("suite")

    wb = sub.add_parser("writeback")
    wb.add_argument("suite")
    wb.add_argument("--report", required=True)
    wb.add_argument("--experiment", required=True)
    wb.add_argument("--dry-run", action="store_true")

    insp = sub.add_parser("inspect")
    insp.add_argument("suite")

    args = p.parse_args(argv)
    suite = _resolve(args.suite)

    if args.cmd == "collect":
        json.dump(suite.collect(), sys.stdout, ensure_ascii=False)
        print()
    elif args.cmd == "publish":
        examples = json.loads(sys.stdin.read())
        r = suite.publish(examples)
        print(f"published {r.example_count} examples sha256={r.sha256}")
    elif args.cmd == "refresh":
        r = suite.publish(suite.collect())
        print(f"refreshed {suite.suite}: {r.example_count} examples sha256={r.sha256}")
    elif args.cmd == "writeback":
        s = suite.writeback_grades(
            report_path=args.report,
            experiment_name=args.experiment,
            dry_run=args.dry_run,
        )
        print(s)
    elif args.cmd == "inspect":
        print(f"suite={suite.suite} dataset_version={suite.dataset_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Write + run + commit**

```bash
uv run pytest tests/eval/test_data_cli.py -v && make check
git add evals/data/__main__.py tests/eval/test_data_cli.py
git commit -m "feat(evals/data): add CLI router (collect/publish/refresh/writeback/inspect)"
```

---

### Task 10: Wire singletons + collapse runtime modules to shims

**Files:** `evals/data/__init__.py` (wire) + three runtime shims

- [ ] **Step 1: Wire `evals/data/__init__.py`**

```python
from evals.data._base import CollectedExample, EvalDataset, PublishResult, WritebackSummary, sha256_examples
from evals.data.review import ReviewDataset
from evals.data.chat import ChatDataset
from evals.data.fix import FixDataset
from evals.data.swt import SwtDataset

REVIEW = ReviewDataset()
CHAT = ChatDataset()
FIX = FixDataset()
SWT = SwtDataset()
DATASETS: dict[str, EvalDataset] = {d.suite: d for d in (REVIEW, CHAT, FIX, SWT)}

__all__ = ["CollectedExample", "DATASETS", "EvalDataset", "FIX", "SWT",
           "REVIEW", "CHAT", "PublishResult", "WritebackSummary",
           "FixDataset", "SwtDataset", "ReviewDataset", "ChatDataset", "sha256_examples"]
```

- [ ] **Step 2: Thin shims (for back-compat until Task 16)**

```python
# evals/runtime/predictions.py  — replace entire file
from evals.data._predictions import *  # noqa: F401,F403
from evals.data._predictions import (  # noqa: F401
    SweBenchPrediction, SwtBenchPrediction, SweQaProAnswer, SweQaProCitation,
    empty_swe_prediction, empty_swt_prediction, prediction_exporter, predictions_path,
)
```

```python
# evals/runtime/langsmith.py  — replace entire file
# NOTE: LangSmithExperiment + configure_tracing_for_dataset are NOT re-exported.
# They are eliminated. Any caller must be migrated.
from evals.data._utils import (  # noqa: F401
    ensure_feedback_config, git_sha, reset_feedback_cache_for_tests, resolve_model_label,
)
```

```python
# evals/runtime/datasets.py  — replace entire file
from evals.data._samples import *  # noqa: F401,F403
from evals.data._samples import (  # noqa: F401
    SWE_QA_PRO_REPO_PATH, issue_row_to_sample, langsmith_dataset,
    load_issue_dataset, qa_example_to_agent_sample, qa_example_to_sample,
    review_example_to_sample,
)
```

- [ ] **Step 3: Run full eval suite**

```bash
uv run pytest tests/eval -v
make check
```

Expected: all legacy tests green through shims.

- [ ] **Step 4: Commit**

```bash
git add evals/data/__init__.py evals/runtime/{predictions,langsmith,datasets}.py
git commit -m "refactor(evals): wire EvalDataset singletons; collapse runtime modules to shims"
```

---

### Task 11: Extend `langsmith_hook.py` — aggregate metrics on `on_task_end`

**Files:** `evals/runtime/langsmith_hook.py` + extend `tests/eval/test_langsmith_hook.py` (rename from `test_langsmith_experiments.py`)

Goal: add aggregate-metric push in `on_task_end`. Also add a guard that verifies `configure_tracing_for_dataset` now runs inside `on_task_start` (not in `build_task`).

- [ ] **Step 1: Extend `on_task_end`**

```python
# evals/runtime/langsmith_hook.py — replace on_task_end
async def on_task_end(self, data: TaskEnd) -> None:
    session = self._sessions.pop(data.eval_id, None)
    if not (session and session.usable):
        return
    if not data.log.results:
        return
    try:
        from langsmith import Client
        client = Client()
        from evals.data._utils import ensure_feedback_config
        for scorer_name, scorer_metrics in (data.log.results.scores or {}).items():
            for metric_name, metric_val in (scorer_metrics.metrics or {}).items():
                if metric_val.value is None:
                    continue
                agg_key = f"{scorer_name}__{metric_name}"
                ensure_feedback_config(client, agg_key, _DEFAULT_FEEDBACK_CONFIG)
                client.create_feedback(
                    run_id=session.project_id,
                    key=agg_key,
                    score=float(metric_val.value),
                    comment=(
                        f"aggregate {metric_name} over "
                        f"{data.log.stats.completed_samples} samples"
                    ),
                    source_info={"source": "inspect_task_end_hook"},
                )
    except Exception as exc:
        logger.warning("langsmith hook: on_task_end aggregate failed: %s", exc)
```

- [ ] **Step 2: Move configure_tracing into `on_task_start`**

Currently `configure_tracing_for_dataset` is called inside `build_task()` in the old design. Since we removed it from `build_task()`, verify it happens in the hook's `on_task_start` instead:

```python
# evals/runtime/langsmith_hook.py — extend on_task_start
async def on_task_start(self, data: TaskStart) -> None:
    spec = data.spec
    spec_metadata = dict(spec.metadata or {})
    dataset_name = spec_metadata.get("dataset_version") or getattr(spec.dataset, "name", None)

    # Redirect LangChain auto-traces to the eval project (was: configure_tracing_for_dataset)
    _redirect_traces_to_eval_project()

    session = _build_session(dataset_name=dataset_name, spec_metadata=spec_metadata)
    if session is None:
        return
    self._sessions[data.eval_id] = session
```

Add `_redirect_traces_to_eval_project()` as a module-private function (extracted from `evals/runtime/__init__.py`):

```python
def _redirect_traces_to_eval_project() -> None:
    import os
    from evals.runtime.config import LANGSMITH_EVAL_PROJECT_ENV
    project = os.environ.get(LANGSMITH_EVAL_PROJECT_ENV)
    if project and os.environ.get("LANGSMITH_API_KEY"):
        os.environ["LANGSMITH_PROJECT"] = project
```

- [ ] **Step 3: Add tests**

```python
# tests/eval/test_langsmith_hook.py (rename from test_langsmith_experiments.py)
# Append these tests:

def test_on_task_end_writes_aggregate_metrics(monkeypatch) -> None:
    # Verify on_task_end calls create_feedback when data.log.results is present
    ...

def test_on_task_end_noop_when_no_results(monkeypatch) -> None:
    ...

def test_on_task_start_redirects_traces(monkeypatch) -> None:
    ...
```

- [ ] **Step 4: Run + `make check` + commit**

```bash
uv run pytest tests/eval -v && make check
git add evals/runtime/langsmith_hook.py tests/eval/test_langsmith_hook.py
git commit -m "feat(langsmith_hook): on_task_end aggregate metrics; trace redirect in on_task_start"
```

---

### Task 12: Rewrite the four task files

**Files:** `evals/tasks/*.py` + `evals/tasks/_review_overlap_scorer.py` + `tests/eval/test_task_wiring.py`

Goal: every task file has zero LangSmith imports. Single `@task` body delegates to `<SUITE>.build_task(...)`.

- [ ] **Step 1: Extract scorer factory**

```python
# evals/tasks/_review_overlap_scorer.py
from evals.scorers.review_judge import judge_verdict as martian_judge_verdict
from evals.scorers.review_overlap import JudgeFn, review_overlap_f1_scorer
from typing import cast

def make_review_overlap_scorer():
    return review_overlap_f1_scorer(cast(JudgeFn, martian_judge_verdict))
```

- [ ] **Step 2: Rewrite the four task files (all zero LangSmith imports)**

```python
# fix_swe_bench.py
from inspect_ai import Task, task
from evals.data import FIX
from evals.solvers.fix import openbot_fix_solver

@task
def fix_swe_bench() -> Task:
    return FIX.build_task(solver=openbot_fix_solver())
```

```python
# test_swt_bench.py
from inspect_ai import Task, task
from evals.data import SWT
from evals.solvers.test_generation import openbot_test_generation_solver

@task
def test_swt_bench() -> Task:
    return SWT.build_task(solver=openbot_test_generation_solver())
```

```python
# chat_swe_qa.py
from inspect_ai import Task, task
from evals.data import CHAT
from evals.scorers.swe_qa_pro import swe_qa_pro_judge_scorer
from evals.solvers.chat import openbot_chat_solver

@task
def chat_swe_qa_pro() -> Task:
    return CHAT.build_task(solver=openbot_chat_solver(), scorer=swe_qa_pro_judge_scorer())
```

```python
# review_martian.py
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

- [ ] **Step 3: Verify no langsmith imports in task files**

```bash
grep -rn "langsmith\|LangSmith\|experiment\|configure_tracing" evals/tasks/ && echo "FAIL" || echo "OK"
```

Expected: `OK` (zero matches).

- [ ] **Step 4: Run + `make check` + commit**

```bash
uv run pytest tests/eval -v && make check
git add evals/tasks/ tests/eval/test_task_wiring.py
git commit -m "refactor(evals/tasks): collapse to pure @task bodies using suite singletons"
```

---

### Task 13: Rewrite Makefile data/writeback targets

```makefile
data-review:
	$(DOPPLER) $(PY) python -m evals.data refresh review

data-chat:
	$(DOPPLER) $(PY) python -m evals.data refresh chat

data-fix:
	$(DOPPLER) $(PY) python -m evals.data refresh fix

data-test:
	$(DOPPLER) $(PY) python -m evals.data refresh swt

writeback-fix:
	# ... existing shell preamble for $$GRADE_FIX_REPORT, $$FIX_EXP_NAME ...
	cd .. && $(DOPPLER) $(PY) python -m evals.data writeback fix \
		--report "$$GRADE_FIX_REPORT" --experiment "$$FIX_EXP_NAME" $(if $(DRY_RUN),--dry-run)

writeback-test:
	# ... existing shell preamble ...
	cd .. && $(DOPPLER) $(PY) python -m evals.data writeback swt \
		--report "$$GRADE_REPORT" --experiment "$$EXP_NAME" $(if $(DRY_RUN),--dry-run)
```

```bash
uv run pytest tests/eval/test_makefile.py -v && make check
git add evals/Makefile tests/eval/test_makefile.py
git commit -m "refactor(evals/Makefile): point data/writeback at unified CLI"
```

---

### Task 14: Delete `evals/scripts/` — six files

Precondition: `git grep -nE "evals\.scripts\.(build_|writeback_)"` returns zero hits.

Inline `collect()` bodies in suite classes (remove transitional imports added in Tasks 5/6).

```bash
git rm evals/scripts/build_{review_martian,chat_swe_qa_pro,swe_bench_verified,swt_bench_verified}_dataset.py
git rm evals/scripts/writeback_{swe,swt}_grades.py
uv run pytest tests/eval -v && make check
git commit -m "refactor(evals): delete legacy build_*/writeback_* scripts"
```

---

### Task 15: Delete `evals/runtime/{datasets,predictions,langsmith}.py` shims

Precondition: `git grep -nE "evals\.runtime\.(datasets|predictions|langsmith)"` returns zero hits.

```bash
git rm evals/runtime/{datasets,predictions,langsmith}.py
uv run pytest tests/eval -v && make check
git commit -m "refactor(evals/runtime): drop predictions/langsmith/datasets shims"
```

---

### Task 16: Replace legacy test files

Precondition: all tests in the four legacy files are covered by `test_data_*.py`.

```bash
git rm tests/eval/test_{datasets,predictions,inspect_helpers,langsmith_experiments}.py
uv run pytest tests/eval -v && make check
git add -A tests/eval
git commit -m "test(evals): replace per-module tests with test_data_* suite"

# Archive the plan and spec
mv docs/superpowers/plans/2026-05-24-evals-data-unification.md docs/_archive/superpowers/
mv docs/superpowers/specs/2026-05-24-evals-data-unification-design.md docs/_archive/superpowers/
git add docs/
git commit -m "docs: archive eval-data-unification plan and spec"
```

---

## Self-Review Checklist

- [ ] `grep -rn "langsmith\|LangSmith" evals/tasks/` returns zero hits.
- [ ] `grep -rn "LangSmithExperiment\|configure_tracing_for_dataset\|build_export_experiment" evals/data/` returns zero hits.
- [ ] `git grep -n "evals.runtime.predictions"` returns nothing.
- [ ] `git grep -n "evals.runtime.langsmith"` returns nothing.
- [ ] `git grep -n "evals.runtime.datasets"` returns nothing.
- [ ] `git grep -n "from evals.scripts"` returns nothing.
- [ ] `make check` green on final commit.
- [ ] `python -m evals.data inspect fix` prints dataset info (smoke test).
- [ ] Each of the four task files contains exactly one `@task` function and zero LangSmith imports.

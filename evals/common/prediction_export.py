"""Accumulate solver outputs into an official benchmark ``predictions.jsonl``.

Inspect AI runs samples concurrently; each solver completes with a
:class:`evals.common.predictions.SweBenchPrediction` (or
:class:`SwtBenchPrediction`) attached to ``state.metadata['prediction']``.
This module ships a thin scorer that:

1. Validates the prediction shape (re-parses the JSON through pydantic so
   schema drift is caught at scoring time, not at offline-harness time).
2. Appends the row to a file under ``evals/outputs/{dataset_version}/
   {experiment_name}.predictions.jsonl``.
3. Returns a sentinel :class:`Score` so Inspect's log viewer still has a
   row per sample and the LangSmith Experiment wrapper still fires.

Why a scorer rather than a hook: Inspect's scorer protocol is the only
official extension point per sample, and we want the per-sample finalize
behaviour to inherit the same retries / timeouts as other scorers. The
"score" itself carries no grading signal — actual grading happens
**offline** by piping the assembled JSONL to the upstream Docker harness:

    python -m swebench.harness.run_evaluation \
        --dataset_name princeton-nlp/SWE-bench_Verified \
        --predictions_path evals/outputs/fix_swe_bench_verified/<name>.predictions.jsonl \
        --max_workers 8 --run_id openbot-{date}

For SWT-Bench, swap to ``swtbench.run_evaluation`` with the test-patch flag.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from inspect_ai.scorer import Score, Scorer, Target, mean, scorer
from inspect_ai.solver import TaskState
from pydantic import BaseModel, ValidationError

from evals.common.config import get_eval_config

logger = logging.getLogger(__name__)


def _output_root() -> Path:
    """Resolve the directory where prediction JSONL files are written.

    Reads :class:`~evals.common.config.PredictionsSettings.output_dir`
    via :func:`~evals.common.config.get_eval_config` — pydantic-settings
    binds ``OPENBOT_PREDICTIONS_DIR`` and validates the path.
    """
    return get_eval_config().predictions.output_dir


def _now_slug() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S")


class _AppendWriter:
    """Thread-safe append-only JSONL writer keyed by output path.

    Inspect runs samples concurrently; pydantic models are immutable but
    file ``write`` is not, so we serialise appends behind a per-path
    lock. The lock dict is module-global so multiple solvers writing to
    the same file (e.g. the same dataset under two solver_family names)
    don't trample each other.
    """

    _locks: ClassVar[dict[Path, threading.Lock]] = {}
    _root_lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def _lock_for(cls, path: Path) -> threading.Lock:
        with cls._root_lock:
            if path not in cls._locks:
                cls._locks[path] = threading.Lock()
            return cls._locks[path]

    @classmethod
    def append(cls, path: Path, row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, sort_keys=True)
        lock = cls._lock_for(path)
        with lock, path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def predictions_path(
    *,
    dataset_version: str,
    run_label: str,
    root: Path | None = None,
) -> Path:
    """Compute the output JSONL path for ``dataset_version`` x ``run_label``.

    The path layout (``<root>/<dataset_version>/<run_label>.predictions.jsonl``)
    matches what the SWE-bench/SWT-Bench harness expects via
    ``--predictions_path``.
    """
    base = (root or _output_root()) / dataset_version
    safe_label = run_label.replace(os.sep, "_")
    return base / f"{safe_label}.predictions.jsonl"


def prediction_exporter(
    *,
    dataset_version: str,
    schema: type[BaseModel],
    run_label: str | None = None,
    metadata_key: str = "prediction",
) -> Scorer:
    """Return an Inspect ``@scorer`` that validates + appends predictions.

    Args:
        dataset_version: Logical dataset name (``fix_swe_bench_verified``,
            ``test_swt_bench_verified``). Used in the output path.
        schema: Pydantic model the solver's prediction must validate as.
            One of :class:`SweBenchPrediction` / :class:`SwtBenchPrediction`.
        run_label: Optional label folded into the filename. Defaults to a
            UTC timestamp; pass the LangSmith experiment session name for
            traceable filenames.
        metadata_key: Key under ``state.metadata`` where the solver stashed
            its prediction (either a model instance or its ``.model_dump()``).
    """
    effective_label = run_label or _now_slug()
    out_path = predictions_path(
        dataset_version=dataset_version,
        run_label=effective_label,
    )

    @scorer(metrics=[mean()], name="swe_export_ok")
    def _scorer() -> Scorer:
        async def _score(state: TaskState, _target: Target) -> Score:
            raw = (state.metadata or {}).get(metadata_key)
            if raw is None:
                return Score(
                    value=0,
                    answer="",
                    explanation="solver did not produce a prediction",
                    metadata={"export_path": str(out_path)},
                )
            try:
                row = schema.model_validate(raw)
            except ValidationError as exc:
                return Score(
                    value=0,
                    answer="",
                    explanation=f"prediction failed schema validation: {exc}",
                    metadata={"export_path": str(out_path)},
                )

            # Empty / whitespace-only patch == agent produced no edits. The
            # SWE-bench / SWT-Bench grader rejects empty patches, so an
            # export-sentinel of 1 here would be actively misleading on the
            # LangSmith dashboard (it'd look like pass@1, which it isn't —
            # real grading happens offline). Score it 0 with an explicit
            # explanation so the no-edits case is visible.
            patch = getattr(row, "model_patch", "") or ""
            if not patch.strip():
                return Score(
                    value=0,
                    answer=row.model_dump_json(),
                    explanation=(
                        "prediction has empty model_patch (agent produced no "
                        "edits — likely hit a model/tool/recursion budget "
                        "before reaching a fix); skipping append to keep "
                        "predictions.jsonl free of empty rows"
                    ),
                    metadata={"export_path": str(out_path), "empty_patch": True},
                )

            # File IO on a worker thread so we don't block the inspect
            # event loop on disk fsync.
            await asyncio.to_thread(_AppendWriter.append, out_path, row.model_dump())

            # value=1 is a *sentinel* meaning "prediction shape valid and
            # exported to JSONL" — NOT a pass@1 result. The real pass@1
            # comes from running the upstream Docker harness offline against
            # the exported JSONL (see this module's docstring). The feedback
            # key wired in `LangSmithExperiment.wrap()` is renamed to
            # ``swe_export_ok`` accordingly to prevent dashboard misreads.
            return Score(
                value=1,
                answer=row.model_dump_json(),
                explanation=(
                    "non-empty prediction validated and appended to JSONL "
                    "(export sentinel; run upstream harness for real pass@1)"
                ),
                metadata={"export_path": str(out_path), "empty_patch": False},
            )

        return _score

    return _scorer()

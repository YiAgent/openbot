"""Structured-output schemas + JSONL exporter for eval solvers.

This module owns two coupled concerns the upstream benchmark harnesses
need together:

1. **Schemas** — Pydantic models the agent's terminal step *must* produce
   (also bound to deepagents' ``response_format=``). These are the
   on-the-wire format for the official SWE-bench / SWT-Bench harnesses,
   so field names match those harnesses verbatim — do not rename without
   updating the upstream harness flags.
2. **Exporter scorer** — an Inspect ``@scorer`` that validates each
   prediction against its schema and appends valid rows to the official
   ``predictions.jsonl``.

Three schemas live here:

- :class:`SweBenchPrediction` — SWE-bench Verified (``model_patch`` is a
  unified diff that touches *production* code).
- :class:`SwtBenchPrediction` — SWT-Bench Verified. SWT-Bench's official
  ``run_evaluation.py`` consumes the **same** record shape as SWE-bench —
  the ``model_patch`` field contains a test-only diff.
- :class:`SweQaProAnswer` — SWE-QA-Pro free-form answer plus structured
  evidence citations (mirrors Appendix D ``<finish>`` block content).

Convenience: :func:`empty_swe_prediction` builds a no-op prediction when
the agent failed to make any edits — keeps the downstream JSONL valid
rather than skipping the row (which would be silently treated as 0/N
by the official harness in either case, but the explicit empty patch
makes the failure visible in the leaderboard CSV).

Why a scorer rather than a hook for the exporter: Inspect's scorer
protocol is the only official extension point per sample, and we want
the per-sample finalize behaviour to inherit the same retries / timeouts
as other scorers. The "score" itself carries no grading signal — actual
grading happens **offline** by piping the assembled JSONL to the upstream
Docker harness:

    python -m swebench.harness.run_evaluation \\
        --dataset_name princeton-nlp/SWE-bench_Verified \\
        --predictions_path evals/results/predictions/fix_swe_bench_verified/<name>.predictions.jsonl \\
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
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evals.runtime.config import get_eval_config

logger = logging.getLogger(__name__)


# ─── Schemas ────────────────────────────────────────────────────────────────


class _StrictModel(BaseModel):
    """Forbid extra keys so schema drift fails loudly at parse time."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SweBenchPrediction(_StrictModel):
    """Single row of an SWE-bench ``predictions.jsonl``.

    Field names follow the official harness:
    https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/run_evaluation.py
    """

    instance_id: str = Field(description="Dataset row id, e.g. astropy__astropy-12907.")
    model_name_or_path: str = Field(
        description="Free-form identifier of the model + scaffold combo. "
        "Used for naming the result directory in the upstream harness.",
    )
    model_patch: str = Field(
        description="Unified diff of the agent's edits vs the base commit. "
        "Empty string means no edits — kept for completeness so the row "
        "still validates."
    )


class SwtBenchPrediction(_StrictModel):
    """Single row of an SWT-Bench ``predictions.jsonl``.

    SWT-Bench shares the SWE-bench input schema; the ``model_patch`` field
    is expected to touch *only* test files (the upstream grader rejects
    non-test edits). Kept as a distinct class so we can extend it later
    without breaking SWE-bench callers.
    """

    instance_id: str
    model_name_or_path: str
    model_patch: str


class SweQaProCitation(_StrictModel):
    """Single evidence citation in an SWE-QA-Pro answer.

    Lines are 1-indexed, inclusive. ``relative_path`` is relative to the
    repo root inside ``/workspace`` so it matches the paper's grading
    convention (Appendix D "<relative_path>: line <start>-<end>").
    """

    relative_path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class SweQaProAnswer(_StrictModel):
    """Final answer + structured citations for one SWE-QA-Pro question.

    The paper's grader scores only the answer body; the structured citations
    are for our own offline analysis (citation-rate is a useful quality
    signal even though it doesn't enter the public score).
    """

    answer: str = Field(description="The natural-language final answer.")
    citations: list[SweQaProCitation] = Field(default_factory=list)


def empty_swe_prediction(
    *,
    instance_id: str,
    model_name_or_path: str,
) -> SweBenchPrediction:
    """Build a no-edit SWE-bench prediction row.

    Used when the agent errored out or produced no diff — keeps the
    ``predictions.jsonl`` round-trippable so the leaderboard still shows
    the instance as attempted-and-failed instead of silently absent.
    """
    return SweBenchPrediction(
        instance_id=instance_id,
        model_name_or_path=model_name_or_path,
        model_patch="",
    )


def empty_swt_prediction(
    *,
    instance_id: str,
    model_name_or_path: str,
) -> SwtBenchPrediction:
    """SWT-Bench counterpart to :func:`empty_swe_prediction`."""
    return SwtBenchPrediction(
        instance_id=instance_id,
        model_name_or_path=model_name_or_path,
        model_patch="",
    )


# ─── JSONL exporter scorer ──────────────────────────────────────────────────


def _output_root() -> Path:
    """Resolve the directory where prediction JSONL files are written.

    Reads :class:`~evals.runtime.config.PredictionsSettings.output_dir`
    via :func:`~evals.runtime.config.get_eval_config` — pydantic-settings
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
    scorer_name: str = "swe_export_ok",
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

    @scorer(metrics=[mean()], name=scorer_name)
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

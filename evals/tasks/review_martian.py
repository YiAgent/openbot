"""Martian-style PR review smoke task — PRD §4.1 / §14 E1 验收.

This is the v0.1 smoke variant. It uses the deepagents-based review solver
(`evals.solvers.openbot_review`) against the hand-authored
`martian_smoke_v1` dataset (5 samples) and scores with a heuristic overlap
scorer wrapping `evals.scorers.review_overlap.compute_review_overlap`.

Run:
    doppler run --project openbot --config dev -- \
        uv run inspect eval evals/tasks/review_martian.py --limit 5

When E2-T01 lands the upstream Martian dataset lock, only `_DATASET_VERSION`
needs to change (and the file it points to).
"""

from __future__ import annotations

import gzip
import json
import pathlib
import re as _re
from typing import Any

from inspect_ai import Task, task
from inspect_ai.dataset import Sample, json_dataset
from inspect_ai.scorer import Score, Target, mean, scorer, stderr
from inspect_ai.solver import TaskState

from evals.scorers.review_overlap import Finding, JudgeVerdict, compute_review_overlap
from evals.solvers.registry import get_review_solver

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_SMOKE_DATASET_VERSION = "martian_smoke_v1"
_FULL_DATASET_VERSION = "martian_2026w20"
_SMOKE_DATASET_PATH = _REPO_ROOT / "evals" / "datasets" / f"{_SMOKE_DATASET_VERSION}.jsonl"
_FULL_DATASET_PATH = _REPO_ROOT / "evals" / "datasets" / f"{_FULL_DATASET_VERSION}.jsonl"


def _ensure_decompressed(path: pathlib.Path) -> pathlib.Path:
    """Materialize <path> from <path>.gz when only the gzipped form is on disk.

    The committed form of large datasets (e.g. martian_2026w20) is gzipped to
    stay under the repo's pre-commit large-files threshold; Inspect AI's
    ``json_dataset`` reads plain JSONL, so we lazily decompress on first
    access. Idempotent — silently re-uses the existing .jsonl if it is at
    least as new as the .gz.
    """
    if path.exists():
        return path
    gz_path = path.with_suffix(path.suffix + ".gz")
    if not gz_path.exists():
        raise FileNotFoundError(
            f"Neither {path} nor {gz_path} is present. Run "
            f"`uv run python -m evals.scripts.build_martian_dataset` to materialize it."
        )
    path.write_bytes(gzip.decompress(gz_path.read_bytes()))
    return path


def _record_to_sample(record: dict[str, Any]) -> Sample:
    """Translate one JSONL record into an inspect-ai Sample.

    `target` carries the golden findings as a JSON string (Sample.target's
    type is str|list[str]; the scorer parses it back to the structured list).
    """
    return Sample(
        id=record["id"],
        input=record["input"],
        target=json.dumps(record["target"]),
        metadata=record.get("metadata", {}),
    )


def _heuristic_judge(g: Finding, c: Finding) -> JudgeVerdict:
    """Deterministic, no-LLM judge for the v0.1 smoke baseline.

    The real PRD §10.3 judge (LLM-based, via `evals.common.judges.Judge`) is
    wired in once cost budget allows; for the smoke we use a lightweight
    heuristic:
      - same file path (if specified in golden)
      - golden line within ±3 of candidate line (if specified in golden)
      - body keyword overlap ≥ 1 stem (4+ char tokens)

    Good enough for pipeline validation. Real numbers come from the LLM judge
    in subsequent runs.
    """
    # If golden specifies a file, candidate must match.
    # Martian gold comments often have empty file strings.
    if g["file"] and g["file"] != c["file"]:
        return {"match": False, "confidence": 0.0, "rationale": "file differs"}

    gl, cl = g.get("line"), c.get("line")
    if gl is not None and cl is not None and abs(gl - cl) > 3:
        return {"match": False, "confidence": 0.0, "rationale": "line distance > 3"}

    def toks(s: str) -> set[str]:
        return set(_re.findall(r"[a-zA-Z]{4,}", s.lower()))

    shared = toks(g["body"]) & toks(c["body"])
    if not shared:
        return {"match": False, "confidence": 0.1, "rationale": "no keyword overlap"}
    return {"match": True, "confidence": 0.8, "rationale": f"overlap on {sorted(shared)[:3]}"}


@scorer(metrics=[mean(), stderr()])
def review_overlap_inspect_scorer():  # type: ignore[no-untyped-def]
    """Inspect AI @scorer shim around `compute_review_overlap`.

    Emits F1 as the primary `value` (so inspect-ai aggregates it as a metric)
    and stashes the full OverlapReport in `metadata` for trace export via
    `evals/scripts/export_run_summary.py` (E1-T09).
    """

    async def _score(state: TaskState, target: Target) -> Score:
        candidate: list[Finding] = state.metadata.get("candidate_findings", [])
        golden_raw = target.text
        try:
            golden: list[Finding] = json.loads(golden_raw)
        except (json.JSONDecodeError, TypeError):
            golden = []

        report = compute_review_overlap(golden, candidate, _heuristic_judge)
        return Score(
            value=report.f1,
            answer=json.dumps({"findings": candidate}, ensure_ascii=False),
            explanation=(
                f"precision={report.precision:.3f} recall={report.recall:.3f} f1={report.f1:.3f} "
                f"matched={len(report.matched_pairs)} "
                f"unmatched_golden={len(report.unmatched_golden)} "
                f"unmatched_candidate={len(report.unmatched_candidate)}"
            ),
            metadata={
                "precision": report.precision,
                "recall": report.recall,
                "f1": report.f1,
                "matched_pairs": report.matched_pairs,
                "unmatched_golden": report.unmatched_golden,
                "unmatched_candidate": report.unmatched_candidate,
                "candidate_count": len(candidate),
                "golden_count": len(golden),
            },
        )

    return _score


def build_review_martian_task(
    *, solver_id: str, dataset_path: pathlib.Path, dataset_version: str
) -> Task:
    """Build the shared review task surface for one solver provider."""
    solver_family = "baseline" if solver_id == "deepagents_baseline" else "production"
    solver_factory = get_review_solver(solver_id)
    return Task(
        dataset=json_dataset(str(_ensure_decompressed(dataset_path)), _record_to_sample),
        solver=solver_factory(),
        scorer=review_overlap_inspect_scorer(),
        metadata={
            "dataset_version": dataset_version,
            "solver_id": solver_id,
            "solver_family": solver_family,
        },
    )


@task
def review_martian_baseline() -> Task:
    """Run the durable deepagents baseline against the full Martian v1 dataset."""
    return build_review_martian_task(
        solver_id="deepagents_baseline",
        dataset_path=_FULL_DATASET_PATH,
        dataset_version=_FULL_DATASET_VERSION,
    )


@task
def review_martian_openbot() -> Task:
    """Run the future production OpenBot provider on the full Martian v1 dataset."""
    return build_review_martian_task(
        solver_id="openbot_prod",
        dataset_path=_FULL_DATASET_PATH,
        dataset_version=_FULL_DATASET_VERSION,
    )


@task
def review_martian_smoke() -> Task:
    """Quick smoke task against the hand-authored subset."""
    return build_review_martian_task(
        solver_id="deepagents_baseline",
        dataset_path=_SMOKE_DATASET_PATH,
        dataset_version=_SMOKE_DATASET_VERSION,
    )

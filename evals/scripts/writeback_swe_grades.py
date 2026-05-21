"""Write SWE-bench harness verdicts back to LangSmith Experiment Runs.

SWE counterpart to :mod:`evals.scripts.writeback_swt_grades`. The
upstream ``swebench`` package (pip-installed, no vendor needed unlike
SWT) writes one ``<run_id>.<model>.json`` summary report to the
``--report_dir`` after ``run_evaluation`` finishes, with the same
shape SWT uses: ``resolved_ids`` / ``unresolved_ids`` / ``error_ids``.

This script reads that report and attaches ``swe_bench_pass_at_1`` —
the LangSmith feedback key reserved for offline SWE-bench grading — to each matching
prediction Run in the right experiment session.

Resolution semantics (SWE-bench):
  * ``instance_id ∈ resolved_ids``    → 1.0 (FAIL_TO_PASS + PASS_TO_PASS
                                       both green — official pass@1)
  * ``instance_id ∈ unresolved_ids``  → 0.0 (one or more required tests
                                       still failing post-patch)
  * ``instance_id ∈ error_ids``       → skip (harness couldn't run;
                                       no signal, don't pollute dashboard)

Run::

    doppler run --project openbot --config dev -- \\
      uv run python -m evals.scripts.writeback_swe_grades \\
        --evaluation-result openbot-202605...<run_id>.<model>.json \\
        --experiment-name fix_swe_bench_verified-deepagents_baseline-...
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Reserved offline-grading key. Must not be reused by the in-task export
# sentinel scorer (swe_export_ok).
FEEDBACK_KEY = "swe_bench_pass_at_1"
FEEDBACK_CONFIG = {"type": "continuous", "min": 0.0, "max": 1.0}


def _load_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"evaluation report not found: {path}")
    with path.open() as fh:
        return json.load(fh)


def _classify(report: dict[str, Any]) -> dict[str, float | None]:
    """Map instance_id → {1.0, 0.0, None} from the report's id lists."""
    verdict: dict[str, float | None] = {}
    for inst in report.get("resolved_ids", []):
        verdict[inst] = 1.0
    for inst in report.get("unresolved_ids", []):
        verdict[inst] = 0.0
    for inst in report.get("error_ids", []):
        verdict[inst] = None
    return verdict


def _find_runs_by_instance(client: Any, experiment_name: str) -> dict[str, Any]:
    """Return {instance_id: Run} for every sample Run in the experiment."""
    runs_by_id: dict[str, Any] = {}
    for run in client.list_runs(project_name=experiment_name, is_root=True):
        if run.name:
            runs_by_id[run.name] = run
    return runs_by_id


def _write_feedback(
    client: Any,
    run: Any,
    instance_id: str,
    score: float,
    report_path: Path,
) -> None:
    """Attach swe_bench_pass_at_1 feedback to one prediction Run."""
    from evals.inspect.langsmith import ensure_feedback_config

    ensure_feedback_config(client, FEEDBACK_KEY, FEEDBACK_CONFIG)
    client.create_feedback(
        run_id=run.id,
        trace_id=run.trace_id or run.id,
        session_id=run.session_id,
        key=FEEDBACK_KEY,
        score=score,
        value=score,
        comment=(
            f"SWE-bench harness verdict: "
            f"{'resolved (pass@1)' if score == 1.0 else 'unresolved'}. "
            f"Source: {report_path.name}"
        ),
        source_info={
            "source": "offline_harness_writeback",
            "harness": "swe-bench",
            "report": str(report_path),
        },
        extra={
            "metadata": {
                "metric_name": FEEDBACK_KEY,
                "instance_id": instance_id,
                "report_path": str(report_path),
            }
        },
    )


def writeback(
    *,
    report_path: Path,
    experiment_name: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Top-level driver. Returns printable / testable summary."""
    report = _load_report(report_path)
    verdict_by_id = _classify(report)
    if not verdict_by_id:
        return {
            "report": str(report_path),
            "experiment": experiment_name,
            "matched": 0,
            "skipped_error": 0,
            "missing_in_experiment": [],
            "written": 0,
            "dry_run": dry_run,
            "note": "no resolved/unresolved/error instances in report",
        }

    from langsmith import Client

    client = Client()
    runs_by_id = _find_runs_by_instance(client, experiment_name)

    written = 0
    skipped_error = 0
    missing: list[str] = []
    for instance_id, score in verdict_by_id.items():
        if score is None:
            skipped_error += 1
            continue
        run = runs_by_id.get(instance_id)
        if run is None:
            missing.append(instance_id)
            continue
        if dry_run:
            written += 1
            continue
        try:
            _write_feedback(client, run, instance_id, score, report_path)
            written += 1
        except Exception:
            logger.exception("failed feedback write for %s", instance_id)

    return {
        "report": str(report_path),
        "experiment": experiment_name,
        "matched": len(verdict_by_id) - skipped_error,
        "skipped_error": skipped_error,
        "missing_in_experiment": missing,
        "written": written,
        "dry_run": dry_run,
    }


def _infer_experiment_name(report_path: Path) -> str | None:
    """Best-effort: derive experiment name from the report filename.

    swebench writes ``<run_id>.<model_name_or_path>.json`` (run_id
    first, model second — opposite of SWT's harness). When callers
    pass GRADE_RUN_ID matching the LangSmith experiment name, the
    leading dot-segment is the experiment.
    """
    stem = report_path.stem
    if "." not in stem:
        return None
    # Take the leading segment (before the FIRST dot).
    return stem.split(".", 1)[0]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="writeback_swe_grades")
    p.add_argument("--evaluation-result", type=Path, required=True)
    p.add_argument(
        "--experiment-name",
        type=str,
        default=None,
        help=(
            "LangSmith Experiment session name. Defaults to the leading "
            "filename segment before the first dot (matches GRADE_RUN_ID "
            "convention). Pass explicitly if your run_id and experiment "
            "name diverge."
        ),
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    experiment_name = args.experiment_name or _infer_experiment_name(args.evaluation_result)
    if experiment_name is None:
        logger.error(
            "could not infer experiment name from %s; pass --experiment-name",
            args.evaluation_result,
        )
        return 2

    summary = writeback(
        report_path=args.evaluation_result,
        experiment_name=experiment_name,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary["missing_in_experiment"]:
        logger.warning(
            "%d instance(s) had no matching LangSmith Run in experiment %r "
            "— predictions never reached LangSmith for those samples",
            len(summary["missing_in_experiment"]),
            experiment_name,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Write SWT-bench harness verdicts back to LangSmith Experiment Runs.

Closes the loop from PRD §3.1 / §6.1: Inspect AI emits a per-sample
prediction (one LangSmith Run per ``instance_id`` in an Experiment
session, carrying the ``swt_export_ok`` sentinel feedback). The
vendored SWT-bench harness (``evals.third_party.swt_bench``) grades
each prediction offline against the official 6-pass test grid, writing
``evals/logs/swt_bench/results/<model>.<run_id>.json``. This script reads that
report and attaches the **real** ``swt_bench_pass_at_1`` feedback to
the matching prediction Run. That feedback key is reserved for offline
grading writeback.

Resolution semantics (SWT-Bench):
  * ``instance_id ∈ resolved_ids``    → 1.0 (witnesses-to-pass; W2P)
  * ``instance_id ∈ unresolved_ids``  → 0.0 (failed to reproduce the bug)
  * ``instance_id ∈ error_ids``       → skip (harness couldn't run;
                                       no signal, don't pollute dashboard)

Idempotency: writes a NEW feedback row each invocation. LangSmith
allows multiple feedback rows per (run_id, key); the dashboard
displays the most recent. Re-running this script after a fresh harness
pass overlays a new verdict without removing prior ones — that's the
intended audit trail.

Run::

    doppler run --project openbot --config dev -- \\
      uv run python -m evals.scripts.writeback_swt_grades \\
        --evaluation-result evaluation_results/anthropic:glm-4.5-air.smoke-astropy-12907-v9-tcp.json \\
        --experiment-name test_swt_bench_verified-openbot_agent-20260517-042351

If ``--experiment-name`` is omitted, the script derives it from the
predictions filename stem encoded in the report's ``model_name_or_path``
breadcrumbs — but explicit is safer.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Reserved offline-grading key. This name MUST NOT be used by the in-task
# export sentinel scorer.
FEEDBACK_KEY = "swt_bench_pass_at_1"
FEEDBACK_CONFIG = {"type": "continuous", "min": 0.0, "max": 1.0}


def _load_report(path: Path) -> dict[str, Any]:
    """Parse the harness's evaluation_results JSON."""
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
        # Skip — no signal. Caller decides whether to warn.
        verdict[inst] = None
    return verdict


def _find_runs_by_instance(client: Any, experiment_name: str) -> dict[str, Any]:
    """Return {instance_id: Run} for every sample Run in the experiment.

    The in-task scorer creates one Run per sample with ``name =
    instance_id``; we filter on name
    + project_name to map them back. Cheaper than per-instance lookups
    when writing back a 50-sample batch.
    """
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
    """Attach the swt_bench_pass_at_1 feedback to one prediction Run."""
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
            f"SWT-bench harness verdict: "
            f"{'resolved (W2P)' if score == 1.0 else 'unresolved'}. "
            f"Source: {report_path.name}"
        ),
        source_info={
            "source": "offline_harness_writeback",
            "harness": "swt-bench",
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
    """Top-level driver. Returns a summary dict (printable, testable).

    Idempotency policy: append a new feedback row per invocation. Same
    (run_id, key) pair gets a new row each time; LangSmith dashboard
    shows the latest. We deliberately don't dedupe — re-grading after
    a harness fix should produce a fresh audit-trail entry.
    """
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
            # Never block writeback of the rest of the batch on one bad row.
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

    Harness writes ``<model_name_or_path>.<run_id>.json``. The
    ``<run_id>`` we passed via ``make grade-test GRADE_RUN_ID=...``
    matches the LangSmith experiment name when callers stick to the
    convention. When they diverge (smoke runs, ad-hoc IDs) the caller
    MUST pass --experiment-name explicitly.
    """
    stem = report_path.stem  # strips .json
    if "." not in stem:
        return None
    # Take everything after the first dot (so the model_name_or_path's
    # `:` and `/` don't get split prematurely).
    return stem.split(".", 1)[1]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="writeback_swt_grades")
    p.add_argument(
        "--evaluation-result",
        type=Path,
        required=True,
        help="Path to evaluation_results/<model>.<run_id>.json",
    )
    p.add_argument(
        "--experiment-name",
        type=str,
        default=None,
        help=(
            "LangSmith Experiment session name. Defaults to the run_id "
            "encoded in the report filename — pass explicitly if your "
            "harness run_id and Inspect experiment_name diverge."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Match runs + print summary without creating feedback rows.",
    )
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

"""Post-hoc token-usage writeback from LangSmith trace project to experiment runs.

After an Inspect AI eval completes, LangChain auto-tracing has already sent
per-LLM-call traces (with token usage) to the ``LANGSMITH_PROJECT_EVAL``
project.  This script queries those traces, aggregates token usage per
instance_id, and writes the totals back to the corresponding experiment runs
(created by :mod:`evals.runtime.langsmith_hook`).

Usage::

    python -m evals.data writeback-usage \\
        --experiment <langsmith-experiment-name> \\
        [--trace-project <LANGSMITH_PROJECT_EVAL>] \\
        [--dry-run]

The trace project defaults to ``LANGSMITH_PROJECT_EVAL`` env var.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from evals.runtime.config import LANGSMITH_EVAL_PROJECT_ENV

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UsageWritebackSummary:
    """Result of a token-usage writeback pass."""

    instances_updated: int
    instances_with_usage: int
    instances_missing_in_experiment: int
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    dry_run: bool

    def __str__(self) -> str:
        mode = "[DRY RUN] " if self.dry_run else ""
        return (
            f"{mode}Usage writeback: {self.instances_updated} runs updated, "
            f"{self.instances_with_usage} had usage, "
            f"{self.instances_missing_in_experiment} missing in experiment. "
            f"Totals: {self.total_input_tokens:,} in / "
            f"{self.total_output_tokens:,} out / "
            f"{self.total_tokens:,} total tokens."
        )


def _resolve_trace_project(explicit: str | None = None) -> str:
    """Resolve the trace project name from arg > env > fallback."""
    if explicit:
        return explicit
    return os.environ.get(LANGSMITH_EVAL_PROJECT_ENV) or os.environ.get("LANGSMITH_PROJECT") or ""


def _aggregate_usage_from_traces(
    *,
    client: Any,
    trace_project: str,
) -> dict[str, dict[str, int]]:
    """Query LLM runs in the trace project and aggregate token usage per instance_id.

    LangChain auto-tracing propagates ``RunnableConfig.metadata`` to all child
    runs.  The agent runtime sets ``metadata.run_id = instance_id``, so each
    LLM run carries the correlation key.

    Returns a dict keyed by instance_id with aggregated usage values.
    """
    # Aggregate: instance_id → {input_tokens, output_tokens, total_tokens}
    agg: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "llm_call_count": 0,
        }
    )

    # Query all LLM-type runs in the trace project.
    # LangSmith's list_runs supports filtering by run_type.
    run_count = 0
    for run in client.list_runs(
        project_name=trace_project,
        run_type="llm",
    ):
        run_count += 1
        # The instance_id is in the run's metadata (propagated from parent).
        metadata = run.metadata or {}
        instance_id = metadata.get("run_id") or metadata.get("instance_id")
        if not instance_id:
            # Try extracting from the run name or skip.
            continue

        input_tok = run.input_tokens or 0
        output_tok = run.output_tokens or 0
        total_tok = run.total_tokens or (input_tok + output_tok)

        bucket = agg[str(instance_id)]
        bucket["input_tokens"] += input_tok
        bucket["output_tokens"] += output_tok
        bucket["total_tokens"] += total_tok
        bucket["llm_call_count"] += 1

    logger.info(
        "usage_writeback_scanned trace_project=%s llm_runs=%d instances=%d",
        trace_project,
        run_count,
        len(agg),
    )
    return dict(agg)


def run_usage_writeback(
    *,
    experiment_name: str,
    trace_project: str | None = None,
    dry_run: bool = False,
    client: Any | None = None,
) -> UsageWritebackSummary:
    """Aggregate token usage from traces and write back to experiment runs.

    1. Query LLM runs in the trace project, group by instance_id.
    2. Index experiment runs by instance_id.
    3. For each instance with usage, update the experiment run's ``usage`` field.
    """
    if client is None:
        from langsmith import Client

        client = Client()

    resolved_trace_project = _resolve_trace_project(trace_project)
    if not resolved_trace_project:
        raise ValueError(
            "No trace project specified. Set LANGSMITH_PROJECT_EVAL env var "
            "or pass --trace-project."
        )

    # ── Step 1: aggregate usage from traces ────────────────────────────────
    usage_by_instance = _aggregate_usage_from_traces(
        client=client,
        trace_project=resolved_trace_project,
    )

    if not usage_by_instance:
        return UsageWritebackSummary(
            instances_updated=0,
            instances_with_usage=0,
            instances_missing_in_experiment=0,
            total_input_tokens=0,
            total_output_tokens=0,
            total_tokens=0,
            dry_run=dry_run,
        )

    # ── Step 2: index experiment runs by instance_id ───────────────────────
    experiment_runs: dict[str, Any] = {}
    for run in client.list_runs(project_name=experiment_name, is_root=True):
        meta = (run.extra or {}).get("metadata", {})
        iid = meta.get("instance_id")
        if iid:
            experiment_runs[str(iid)] = run

    # ── Step 3: write back usage ───────────────────────────────────────────
    # LangSmith's update_run doesn't accept a `usage` kwarg, so we write
    # token usage into the run's outputs AND as a feedback score.  The
    # outputs approach makes the numbers visible in the experiment table;
    # the feedback approach makes them queryable via the feedback API.
    updated = 0
    missing = 0
    total_in = total_out = total = 0

    for instance_id, usage in usage_by_instance.items():
        if usage["total_tokens"] == 0:
            continue

        run = experiment_runs.get(instance_id)
        if run is None:
            missing += 1
            logger.debug(
                "usage_writeback_missing instance_id=%s not in experiment %s",
                instance_id,
                experiment_name,
            )
            continue

        if dry_run:
            updated += 1
            total_in += usage["input_tokens"]
            total_out += usage["output_tokens"]
            total += usage["total_tokens"]
            continue

        try:
            # Merge token usage into the run's outputs so the experiment
            # table shows the numbers alongside completion / model_patch.
            existing_outputs = dict(run.outputs or {})
            existing_outputs.update(
                {
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "total_tokens": usage["total_tokens"],
                    "llm_call_count": usage["llm_call_count"],
                }
            )
            client.update_run(
                run_id=run.id,
                outputs=existing_outputs,
            )

            # Also create a feedback score for programmatic queries.
            try:
                client.create_feedback(
                    run_id=str(run.id),
                    key="token_usage",
                    score=float(usage["total_tokens"]),
                    comment=(
                        f"{usage['input_tokens']:,} in / "
                        f"{usage['output_tokens']:,} out / "
                        f"{usage['llm_call_count']} LLM calls"
                    ),
                    source_info={"source": "usage_writeback"},
                )
            except Exception as fb_exc:
                logger.debug(
                    "usage_writeback_feedback_failed instance_id=%s: %s",
                    instance_id,
                    fb_exc,
                )

            updated += 1
            total_in += usage["input_tokens"]
            total_out += usage["output_tokens"]
            total += usage["total_tokens"]
        except Exception as exc:
            logger.warning(
                "usage_writeback_failed instance_id=%s run_id=%s: %s",
                instance_id,
                run.id,
                exc,
            )

    return UsageWritebackSummary(
        instances_updated=updated,
        instances_with_usage=len(usage_by_instance),
        instances_missing_in_experiment=missing,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        total_tokens=total,
        dry_run=dry_run,
    )

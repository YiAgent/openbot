"""Generic offline-grade writeback driver.

Replaces writeback_swe_grades.py / writeback_swt_grades.py.
Suite supplies `feedback_key: str` and static `classify(report)` generator.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

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

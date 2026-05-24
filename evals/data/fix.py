"""FixDataset — SWE-bench Verified fix benchmark suite."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar

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

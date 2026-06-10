"""ReviewDataset — Martian code-review benchmark suite."""

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
        raise NotImplementedError(
            "ReviewDataset.collect() is not available — the build_review_martian_dataset "
            "script was removed in the evals refactor (Phase 1+2+3). Use `build_task()` "
            "to create Inspect AI tasks directly, or load the dataset from HuggingFace."
        )

    def example_to_sample(self, example: Any) -> Any:
        return review_example_to_sample(example)

    def build_task(
        self,
        *,
        solver: Any,
        scorer: Scorer | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Task:
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

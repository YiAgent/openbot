"""ChatDataset — SWE-QA-Pro grounded chat benchmark suite."""

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
        # Transitional: logic inlined in Task 15 when the build script is deleted.
        from evals.scripts import build_chat_swe_qa_pro_dataset as _s

        revision = _s.HF_REVISION
        samples = _s._collect_samples(revision)
        sha = _s._sha256_of_samples(samples)
        return [
            CollectedExample(
                inputs=ex["inputs"],
                outputs=ex.get("outputs"),
                metadata=ex.get("metadata") or {},
            )
            for row in samples
            for ex in [_s._row_to_example(row, sha, revision)]
        ]

    def example_to_sample(self, example: Any) -> Any:
        return qa_example_to_agent_sample(example)

    def build_task(
        self,
        *,
        solver: Any,
        scorer: Scorer | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Task:
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

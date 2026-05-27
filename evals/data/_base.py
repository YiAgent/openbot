"""EvalDataset ABC and shared data types.

build_task() is abstract and MUST return a pure inspect_ai.Task.
No LangSmith imports belong here — all observability is owned by langsmith_hook.py.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar, TypedDict

from inspect_ai.dataset import MemoryDataset
from inspect_ai.scorer import Scorer


class CollectedExample(TypedDict):
    inputs: dict[str, Any]
    outputs: dict[str, Any] | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class PublishResult:
    dataset_id: str
    dataset_name: str
    example_count: int
    sha256: str


@dataclass(frozen=True)
class WritebackSummary:
    feedback_written: int
    runs_matched: int
    instances_unmatched: int
    dry_run: bool


def sha256_examples(examples: Iterable[CollectedExample]) -> str:
    digest = hashlib.sha256()
    for ex in examples:
        digest.update(json.dumps(dict(ex), sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


class EvalDataset(ABC):
    suite: ClassVar[str]
    dataset_version: ClassVar[str]
    instance_id_field: ClassVar[str] = "id"
    description: ClassVar[str] = ""

    @abstractmethod
    def collect(self) -> list[CollectedExample]: ...

    @abstractmethod
    def example_to_sample(self, example: Any) -> Any: ...

    @abstractmethod
    def build_task(
        self,
        *,
        solver: Any,
        scorer: Scorer | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Any: ...

    def publish(
        self,
        examples: list[CollectedExample],
        *,
        client: Any | None = None,
        chunk_size: int = 100,
    ) -> PublishResult:
        if client is None:
            from langsmith import Client

            client = Client()
        try:
            ds = client.read_dataset(dataset_name=self.dataset_version)
        except Exception:
            # LangSmith SDK raises LangSmithNotFoundError (not LookupError)
            # when the dataset doesn't exist. Catch all exceptions to be
            # resilient to SDK version changes.
            ds = client.create_dataset(
                dataset_name=self.dataset_version,
                description=self.description,
            )
        for start in range(0, len(examples), chunk_size):
            batch = examples[start : start + chunk_size]
            client.create_examples(
                inputs=[e["inputs"] for e in batch],
                outputs=[e.get("outputs") for e in batch],
                metadata=[e.get("metadata") or {} for e in batch],
                dataset_id=str(ds.id),
            )
        return PublishResult(
            dataset_id=str(ds.id),
            dataset_name=self.dataset_version,
            example_count=len(examples),
            sha256=sha256_examples(examples),
        )

    def load_for_inspect(self) -> MemoryDataset:
        from evals.data import _samples

        return _samples.langsmith_dataset(self.dataset_version, converter=self.example_to_sample)

    def writeback_grades(
        self,
        *,
        report_path: str,
        experiment_name: str,
        dry_run: bool = False,
        client: Any | None = None,
    ) -> WritebackSummary:
        raise NotImplementedError(
            f"{type(self).__name__} does not support offline grade writeback."
        )

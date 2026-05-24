"""Parametrized lifecycle tests for the EvalDataset hierarchy."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from inspect_ai.dataset import Sample

from evals.data._base import CollectedExample, EvalDataset, PublishResult, sha256_examples


class _FakeSuite(EvalDataset):
    suite: ClassVar[str] = "fake"
    dataset_version: ClassVar[str] = "fake_v1"
    instance_id_field: ClassVar[str] = "id"

    def collect(self) -> list[CollectedExample]:
        return [
            CollectedExample(inputs={"id": "a"}, outputs=None, metadata={}),
            CollectedExample(inputs={"id": "b"}, outputs=None, metadata={}),
        ]

    def example_to_sample(self, example: Any) -> Sample:
        return Sample(id=example.inputs["id"], input="")

    def build_task(
        self,
        *,
        solver: Any,
        scorer: Any = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> Any:
        raise NotImplementedError


def test_sha256_stable_under_key_order() -> None:
    a = [CollectedExample(inputs={"id": "1", "x": 1}, outputs=None, metadata={})]
    b = [CollectedExample(inputs={"x": 1, "id": "1"}, outputs=None, metadata={})]
    assert sha256_examples(a) == sha256_examples(b)


def test_publish_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _FakeSuite()
    seen: list[int] = []

    class _FakeClient:
        def read_dataset(self, dataset_name: str) -> Any:
            raise LookupError(dataset_name)

        def create_dataset(self, dataset_name: str, description: str = "") -> Any:
            return type("D", (), {"id": "00000000-0000-0000-0000-000000000000"})()

        def create_examples(
            self,
            *,
            inputs: list[Any],
            outputs: list[Any],
            metadata: list[Any],
            dataset_id: str,
        ) -> None:
            seen.append(len(inputs))

    examples = [
        CollectedExample(inputs={"id": str(i)}, outputs=None, metadata={}) for i in range(250)
    ]
    result = suite.publish(examples, client=_FakeClient(), chunk_size=100)
    assert isinstance(result, PublishResult)
    assert seen == [100, 100, 50]
    assert result.example_count == 250


def test_writeback_default_raises() -> None:
    suite = _FakeSuite()
    with pytest.raises(NotImplementedError):
        suite.writeback_grades(report_path="x", experiment_name="e")

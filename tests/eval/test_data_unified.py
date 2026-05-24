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


# ---------------------------------------------------------------------------
# Task 5: ReviewDataset
# ---------------------------------------------------------------------------


def test_review_attrs() -> None:
    from evals.data.review import ReviewDataset

    assert ReviewDataset.suite == "review"
    assert ReviewDataset.dataset_version == "martian_2026w20"


def test_review_build_task_raises_without_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from evals.data.review import ReviewDataset

    r = ReviewDataset()
    monkeypatch.setattr(r, "load_for_inspect", MagicMock(return_value=MagicMock()))
    with pytest.raises(ValueError, match="scorer"):
        r.build_task(solver=MagicMock())


def test_review_build_task_has_no_langsmith_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task metadata must not contain langsmith experiment objects."""
    from unittest.mock import MagicMock

    from inspect_ai.dataset import MemoryDataset, Sample
    from inspect_ai.scorer import mean, scorer

    from evals.data.review import ReviewDataset

    @scorer(metrics=[mean()])
    def _noop_scorer():  # type: ignore[no-untyped-def]
        async def _score(state, target):  # type: ignore[no-untyped-def]
            from inspect_ai.scorer import Score

            return Score(value=0)

        return _score

    r = ReviewDataset()
    fake_dataset = MemoryDataset(samples=[Sample(id="x", input="y")], name="fake")
    monkeypatch.setattr(r, "load_for_inspect", MagicMock(return_value=fake_dataset))
    task = r.build_task(solver=MagicMock(), scorer=_noop_scorer())
    for v in (task.metadata or {}).values():
        assert not hasattr(v, "create_run"), "LangSmith object leaked into Task metadata"


# ---------------------------------------------------------------------------
# Task 6: ChatDataset
# ---------------------------------------------------------------------------


def test_chat_attrs() -> None:
    from evals.data.chat import ChatDataset

    assert ChatDataset.suite == "chat"
    assert ChatDataset.dataset_version == "chat_swe_qa_pro_v1"


def test_chat_build_task_raises_without_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from evals.data.chat import ChatDataset

    c = ChatDataset()
    monkeypatch.setattr(c, "load_for_inspect", MagicMock(return_value=MagicMock()))
    with pytest.raises(ValueError, match="scorer"):
        c.build_task(solver=MagicMock())

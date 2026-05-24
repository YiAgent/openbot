"""Contract tests for LangSmith-backed eval dataset materialization."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from evals.runtime import datasets


def _example(**kwargs):  # type: ignore[no-untyped-def]
    return SimpleNamespace(**kwargs)


def test_review_example_to_sample_merges_metadata_and_prefers_input_id() -> None:
    sample = datasets.review_example_to_sample(
        _example(
            id="example-id",
            inputs={
                "id": "input-id",
                "diff": "diff --git a/x b/x",
                "metadata": {"repo": "demo/repo", "shared": "inputs"},
            },
            outputs={"golden_findings": [{"body": "bug"}]},
            metadata={"sample_id": "meta-id", "shared": "example-meta"},
        )
    )

    assert sample.id == "input-id"
    assert sample.input == "diff --git a/x b/x"
    assert sample.target == '[{"body": "bug"}]'
    assert sample.metadata == {
        "repo": "demo/repo",
        "shared": "example-meta",
        "sample_id": "meta-id",
    }


def test_qa_example_to_sample_falls_back_to_metadata_sample_id() -> None:
    sample = datasets.qa_example_to_sample(
        _example(
            id="example-id",
            inputs={"question": "What does it do?", "metadata": {"repo": "demo/repo"}},
            outputs={"answer": "It routes requests."},
            metadata={"sample_id": "meta-id", "commit_id": "abc123"},
        )
    )

    assert sample.id == "meta-id"
    assert sample.input == "What does it do?"
    assert sample.target == "It routes requests."
    assert sample.metadata == {"repo": "demo/repo", "sample_id": "meta-id", "commit_id": "abc123"}


def test_qa_example_to_agent_sample_injects_setup_and_repo_path() -> None:
    sample = datasets.qa_example_to_agent_sample(
        _example(
            id="example-id",
            inputs={
                "id": "input-id",
                "question": "What does the router do?",
                "metadata": {"repo": "pallets/flask"},
            },
            outputs={"answer": "It dispatches requests."},
            metadata={
                "sample_id": "input-id",
                "repo": "pallets/flask",
                "commit_id": "deadbeef",
            },
        )
    )

    assert sample.id == "input-id"
    assert sample.input == "What does the router do?"
    assert sample.target == "It dispatches requests."
    assert sample.metadata["repo"] == "pallets/flask"
    assert sample.metadata["commit_id"] == "deadbeef"
    assert sample.metadata["repo_path"] == datasets.SWE_QA_PRO_REPO_PATH
    # Post-refactor: repo provisioning moved to the Modal sandbox backend,
    # so the Sample no longer carries an inline ``setup`` shell script —
    # the solver constructs a RepoSpec from metadata and lets Modal clone.
    assert sample.setup is None


def test_qa_example_to_agent_sample_rejects_missing_repo_or_commit() -> None:
    with pytest.raises(ValueError, match="non-empty repo and commit_id"):
        datasets.qa_example_to_agent_sample(
            _example(
                id="example-id",
                inputs={"id": "input-id", "question": "?", "metadata": {}},
                outputs={"answer": "y"},
                metadata={"sample_id": "input-id"},
            )
        )


def test_langsmith_dataset_raises_when_dataset_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        def list_datasets(self, *, dataset_name: str):  # type: ignore[no-untyped-def]
            assert dataset_name == "chat_swe_qa_pro_v1"
            return []

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(Client=lambda: _Client()))

    with pytest.raises(RuntimeError, match="not found"):
        datasets.langsmith_dataset("chat_swe_qa_pro_v1", converter=datasets.qa_example_to_sample)


def test_langsmith_dataset_raises_when_dataset_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        def list_datasets(self, *, dataset_name: str):  # type: ignore[no-untyped-def]
            assert dataset_name == "martian_2026w20"
            return [_example(id="dataset-id")]

        def list_examples(self, *, dataset_id: str):  # type: ignore[no-untyped-def]
            assert dataset_id == "dataset-id"
            return []

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(Client=lambda: _Client()))

    with pytest.raises(RuntimeError, match="has no examples"):
        datasets.langsmith_dataset("martian_2026w20")


def test_langsmith_dataset_sorts_samples_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    examples = [
        _example(id="e2", inputs={"id": "b", "diff": "b"}, outputs={}, metadata={}),
        _example(id="e1", inputs={"id": "a", "diff": "a"}, outputs={}, metadata={}),
    ]

    class _Client:
        def list_datasets(self, *, dataset_name: str):  # type: ignore[no-untyped-def]
            assert dataset_name == "martian_2026w20"
            return [_example(id="dataset-id")]

        def list_examples(self, *, dataset_id: str):  # type: ignore[no-untyped-def]
            assert dataset_id == "dataset-id"
            return examples

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(Client=lambda: _Client()))

    dataset = datasets.langsmith_dataset("martian_2026w20")

    assert [sample.id for sample in dataset.samples] == ["a", "b"]
    assert dataset.name == "martian_2026w20"
    assert dataset.location == "langsmith://dataset-id"

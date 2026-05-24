"""Contract tests for Inspect-facing eval helper modules."""

from __future__ import annotations

from evals.data import _samples as hf_datasets


def test_issue_row_to_sample_keeps_only_solver_metadata() -> None:
    sample = hf_datasets.issue_row_to_sample(
        {
            "instance_id": "repo__name-1",
            "problem_statement": "Fix the bug",
            "repo": "repo/name",
            "base_commit": "abc123",
            "version": "1.2.3",
            "FAIL_TO_PASS": ["test_a"],
            "patch": "gold patch",
        }
    )

    assert sample.id == "repo__name-1"
    assert sample.input == "Fix the bug"
    assert sample.target == ""
    assert sample.metadata == {
        "repo": "repo/name",
        "base_commit": "abc123",
        "version": "1.2.3",
    }


def test_load_issue_dataset_sorts_samples(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    rows = [
        {"instance_id": "b", "problem_statement": "b", "repo": "r/b", "base_commit": "2"},
        {"instance_id": "a", "problem_statement": "a", "repo": "r/a", "base_commit": "1"},
    ]
    calls: list[tuple[str, str]] = []

    def _load_dataset(name: str, *, split: str):  # type: ignore[no-untyped-def]
        calls.append((name, split))
        return rows

    monkeypatch.setattr(hf_datasets, "load_dataset", _load_dataset)

    dataset = hf_datasets.load_issue_dataset(
        dataset_name="owner/dataset",
        dataset_version="logical_name",
    )

    assert calls == [("owner/dataset", "test")]
    assert [sample.id for sample in dataset.samples] == ["a", "b"]
    assert dataset.name == "logical_name"
    assert dataset.location == "huggingface://owner/dataset"

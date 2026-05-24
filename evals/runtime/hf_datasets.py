"""HuggingFace dataset adapters for Inspect tasks."""

from __future__ import annotations

from typing import Any

from datasets import load_dataset
from inspect_ai.dataset import MemoryDataset, Sample


def issue_row_to_sample(row: dict[str, Any]) -> Sample:
    """Convert a SWE/SWT-style issue row into the solver-facing sample shape."""
    return Sample(
        id=str(row["instance_id"]),
        input=str(row.get("problem_statement", "")),
        target="",
        metadata={
            "repo": str(row.get("repo", "")),
            "base_commit": str(row.get("base_commit", "")),
            "version": str(row.get("version", "")),
        },
    )


def load_issue_dataset(
    *,
    dataset_name: str,
    dataset_version: str,
    split: str = "test",
) -> MemoryDataset:
    """Load and sort a HuggingFace issue dataset for deterministic Inspect runs."""
    rows = load_dataset(dataset_name, split=split)
    samples = [issue_row_to_sample(dict(row)) for row in rows]
    samples.sort(key=lambda sample: str(sample.id))
    return MemoryDataset(
        samples=samples,
        name=dataset_version,
        location=f"huggingface://{dataset_name}",
    )

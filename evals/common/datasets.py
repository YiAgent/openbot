"""LangSmith-backed dataset loader for Inspect AI tasks.

Source of truth for eval datasets is LangSmith (mirrored from the upstream
benchmark via ``evals/scripts/build_*_dataset.py``). This helper materializes
the LangSmith ``Dataset`` into an Inspect ``MemoryDataset`` so existing
``Task`` plumbing keeps working without any local ``.jsonl`` checked in.

Why a hard fail when the dataset isn't on LangSmith yet? Because the
alternative — silently falling back to a local file — is exactly the
duplication the move to LangSmith is meant to remove. The script that
publishes the dataset (``build_review_martian_dataset.py``) is the only place
that should produce one.
"""

from __future__ import annotations

import json
from typing import Any

from inspect_ai.dataset import MemoryDataset, Sample


def _example_to_sample(example: Any) -> Sample:
    """Convert one LangSmith ``Example`` to an Inspect ``Sample``.

    Mirrors the shape ``upload_examples`` in the build script produces:
      inputs   = {"id": ..., "diff": ..., "metadata": {...}}
      outputs  = {"golden_findings": [...]}
      metadata = {"sample_id": ..., "dataset_version": ..., ...}
    """
    inputs = getattr(example, "inputs", None) or {}
    outputs = getattr(example, "outputs", None) or {}
    meta = getattr(example, "metadata", None) or {}
    sample_id = inputs.get("id") or meta.get("sample_id") or str(getattr(example, "id", ""))
    return Sample(
        id=sample_id,
        input=inputs.get("diff", ""),
        # Inspect's Sample.target is str | list[str]; the scorer parses JSON
        # back into the structured findings list.
        target=json.dumps(outputs.get("golden_findings", [])),
        metadata={**(inputs.get("metadata") or {}), **meta},
    )


def langsmith_dataset(dataset_version: str) -> MemoryDataset:
    """Materialize the LangSmith dataset named ``dataset_version`` as a MemoryDataset.

    Raises ``RuntimeError`` with an actionable hint when the dataset is not
    published yet — the caller is expected to run the corresponding build
    script first.
    """
    from langsmith import Client

    client = Client()
    ds = next((d for d in client.list_datasets(dataset_name=dataset_version)), None)
    if ds is None:
        raise RuntimeError(
            f"LangSmith dataset {dataset_version!r} not found. "
            f"Publish it with `uv run python -m evals.scripts.build_{dataset_version.split('_')[0]}_dataset` "
            f"(or pass --dataset {dataset_version} to the matching build script)."
        )

    examples = list(client.list_examples(dataset_id=ds.id))
    if not examples:
        raise RuntimeError(
            f"LangSmith dataset {dataset_version!r} ({ds.id}) has no examples. "
            f"Re-run the build script with --force."
        )

    samples = [_example_to_sample(ex) for ex in examples]
    # Inspect's MemoryDataset preserves insertion order; sort for deterministic
    # `--limit N` selection across runs.
    samples.sort(key=lambda s: str(s.id))
    return MemoryDataset(samples=samples, name=dataset_version, location=f"langsmith://{ds.id}")

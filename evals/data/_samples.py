"""Dataset loaders for Inspect AI tasks.

Two sources, two shapes:

- **LangSmith** is the source of truth for eval datasets, mirrored from
  upstream benchmarks via ``evals/scripts/build_*_dataset.py``. We
  materialize a LangSmith ``Dataset`` into an Inspect ``MemoryDataset`` so
  ``Task`` plumbing keeps working without any local ``.jsonl`` checked in.
- **HuggingFace** loaders cover SWE/SWT-bench-style "issue rows" where
  the upstream dataset is the canonical artifact and LangSmith mirroring
  isn't necessary.

Each LangSmith task family has a different example shape (review uses
``inputs.diff`` / ``outputs.golden_findings``; chat uses
``inputs.question`` / ``outputs.answer``), so callers pass a ``converter``
to translate LangSmith ``Example`` → Inspect ``Sample``. Three ready-made
converters live below.

Why hard-fail when a LangSmith dataset isn't published yet? Because the
alternative — silently falling back to a local file — is exactly the
duplication the move to LangSmith is meant to remove. The matching
``build_*_dataset.py`` is the only place that produces one.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from datasets import load_dataset
from inspect_ai.dataset import MemoryDataset, Sample

ExampleConverter = Callable[[Any], Sample]


def _coerce_attr(example: Any, name: str) -> dict[str, Any]:
    """Pull a dict-like attr off a LangSmith Example, defaulting to ``{}``."""
    return getattr(example, name, None) or {}


def review_example_to_sample(example: Any) -> Sample:
    """LangSmith Example → Inspect Sample for code-review datasets.

    Expected payload (matches ``build_review_martian_dataset.py``):
      inputs   = {"id": ..., "diff": ..., "metadata": {...}}
      outputs  = {"golden_findings": [...]}
    Target is JSON-encoded so the review_overlap scorer can decode it back.
    """
    inputs = _coerce_attr(example, "inputs")
    outputs = _coerce_attr(example, "outputs")
    meta = _coerce_attr(example, "metadata")
    sample_id = inputs.get("id") or meta.get("sample_id") or str(getattr(example, "id", ""))
    return Sample(
        id=sample_id,
        input=inputs.get("diff", ""),
        target=json.dumps(outputs.get("golden_findings", [])),
        metadata={**(inputs.get("metadata") or {}), **meta},
    )


def qa_example_to_sample(example: Any) -> Sample:
    """LangSmith Example → Inspect Sample for free-form QA datasets.

    Expected payload (matches ``build_chat_swe_qa_pro_dataset.py``):
      inputs   = {"id": ..., "question": ..., "metadata": {...}}
      outputs  = {"answer": <gold reference str>}
    Target is the plain reference string — the LLM judge scorer compares
    candidate vs target directly.
    """
    inputs = _coerce_attr(example, "inputs")
    outputs = _coerce_attr(example, "outputs")
    meta = _coerce_attr(example, "metadata")
    sample_id = inputs.get("id") or meta.get("sample_id") or str(getattr(example, "id", ""))
    return Sample(
        id=sample_id,
        input=inputs.get("question", ""),
        target=str(outputs.get("answer", "")),
        metadata={**(inputs.get("metadata") or {}), **meta},
    )


# Where the repo lives inside the Modal sandbox. Matches the agent-side
# convention used by every SWE/SWT solver (``/workspace``); the +Agent
# variant overrides the paper's Appendix D ``/testbed`` literal at the
# prompt-template level so the agent reads from the right path.
SWE_QA_PRO_REPO_PATH = "/workspace"


def qa_example_to_agent_sample(example: Any) -> Sample:
    """LangSmith Example → Inspect Sample for SWE-QA-Pro +Agent runs.

    Same shape as :func:`qa_example_to_sample`, but the solver is expected
    to spin up its own Modal sandbox using the ``repo`` + ``commit_id``
    metadata fields (cloning into ``/workspace``). No ``setup=`` script is
    emitted here — repo provisioning is the Modal backend's responsibility,
    cleanly decoupled from any evaluation-side sandbox.
    """
    inputs = _coerce_attr(example, "inputs")
    outputs = _coerce_attr(example, "outputs")
    meta = _coerce_attr(example, "metadata")
    sample_meta = {**(inputs.get("metadata") or {}), **meta}
    repo = str(sample_meta.get("repo", ""))
    commit_id = str(sample_meta.get("commit_id", ""))
    if not repo or not commit_id:
        raise ValueError(
            f"qa_example_to_agent_sample requires non-empty repo and commit_id "
            f"in example metadata (got repo={repo!r}, commit_id={commit_id!r}). "
            f"Re-publish the dataset via build_chat_swe_qa_pro_dataset."
        )
    sample_id = inputs.get("id") or sample_meta.get("sample_id") or str(getattr(example, "id", ""))
    return Sample(
        id=sample_id,
        input=inputs.get("question", ""),
        target=str(outputs.get("answer", "")),
        metadata={**sample_meta, "repo_path": SWE_QA_PRO_REPO_PATH},
    )


def langsmith_dataset(
    dataset_version: str,
    *,
    converter: ExampleConverter = review_example_to_sample,
) -> MemoryDataset:
    """Materialize the LangSmith dataset named ``dataset_version`` as a MemoryDataset.

    ``converter`` translates each LangSmith ``Example`` to an Inspect
    ``Sample`` — defaults to the review shape for backward compatibility
    with existing call sites; chat / QA tasks pass ``qa_example_to_sample``.

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

    samples = [converter(ex) for ex in examples]
    # MemoryDataset preserves insertion order; sort for deterministic
    # `--limit N` selection across runs.
    samples.sort(key=lambda s: str(s.id))
    return MemoryDataset(samples=samples, name=dataset_version, location=f"langsmith://{ds.id}")


# ─── HuggingFace loaders ────────────────────────────────────────────────────


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

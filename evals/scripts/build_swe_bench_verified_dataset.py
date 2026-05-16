"""Mirror SWE-bench Verified to a LangSmith dataset — PRD §3.1.

Pulls ``princeton-nlp/SWE-bench_Verified`` from HuggingFace at the same
revision pinned by :func:`inspect_evals.swe_bench.swe_bench` and uploads
the 500 examples to LangSmith dataset ``fix_swe_bench_verified``. Inspect
AI tasks still pull the actual dataset directly from HF at runtime; the
LangSmith mirror exists so per-sample scorer runs can reference the
dataset example by id and show up in the LangSmith Experiments view.

Idempotent: aborts if the dataset already exists, unless ``--force`` is
passed (mirrors ``build_review_martian_dataset.py``).

Usage::

    doppler run --project openbot --config dev -- \\
        uv run python -m evals.scripts.build_swe_bench_verified_dataset
"""

from __future__ import annotations

import argparse
import json
import sys

from inspect_evals.swe_bench.swe_bench import swe_bench as _upstream_swe_bench

DATASET_NAME = "fix_swe_bench_verified"
HF_DATASET = "princeton-nlp/SWE-bench_Verified"
HF_SPLIT = "test"


def _hf_revision() -> str:
    """Read the dataset revision pin from the upstream Inspect AI task default."""
    import inspect

    sig = inspect.signature(_upstream_swe_bench)
    return str(sig.parameters["revision"].default)


def _load_examples() -> list[dict]:
    """Pull the dataset from HF and shape it for LangSmith.

    Each LangSmith example shape:
      - inputs:  the agent-facing surface — issue text + repo + base_commit.
      - outputs: the gold patch (what the model is graded against).
      - metadata: everything else the SWE-bench scorer needs (test_patch,
                  FAIL_TO_PASS, PASS_TO_PASS, version, environment_setup_commit,
                  hints_text, instance_id) + provenance fields.
    """
    from datasets import load_dataset

    revision = _hf_revision()
    print(f"[hf] loading {HF_DATASET} ({HF_SPLIT}) @ {revision[:12]}", file=sys.stderr)
    rows = load_dataset(HF_DATASET, split=HF_SPLIT, revision=revision)

    examples: list[dict] = []
    for row in rows:
        instance_id = str(row["instance_id"])
        examples.append(
            {
                "inputs": {
                    "instance_id": instance_id,
                    "problem_statement": row["problem_statement"],
                    "repo": row["repo"],
                    "base_commit": row["base_commit"],
                    "version": row["version"],
                },
                "outputs": {
                    "gold_patch": row["patch"],
                },
                "metadata": {
                    "instance_id": instance_id,
                    "dataset_version": DATASET_NAME,
                    "hf_dataset": HF_DATASET,
                    "hf_split": HF_SPLIT,
                    "hf_revision": revision,
                    "test_patch": row["test_patch"],
                    "FAIL_TO_PASS": row["FAIL_TO_PASS"],
                    "PASS_TO_PASS": row["PASS_TO_PASS"],
                    "environment_setup_commit": row["environment_setup_commit"],
                    "hints_text": row.get("hints_text", ""),
                    "created_at": row.get("created_at", ""),
                },
            }
        )
    print(f"[hf] loaded {len(examples)} examples", file=sys.stderr)
    return examples


def _publish(examples: list[dict], *, force: bool) -> str:
    from langsmith import Client

    client = Client()
    existing = next((d for d in client.list_datasets(dataset_name=DATASET_NAME)), None)
    if existing and not force:
        print(
            f"FATAL: LangSmith dataset {DATASET_NAME!r} already exists ({existing.id}). "
            f"Re-run with --force to delete and recreate.",
            file=sys.stderr,
        )
        sys.exit(1)
    if existing and force:
        print(
            f"--force: deleting existing dataset {DATASET_NAME} ({existing.id})",
            file=sys.stderr,
        )
        client.delete_dataset(dataset_id=existing.id)

    revision = _hf_revision()
    ds = client.create_dataset(
        dataset_name=DATASET_NAME,
        description=(
            f"SWE-bench Verified mirror — {HF_DATASET} ({HF_SPLIT} split) at HF "
            f"revision {revision[:12]}. Source of truth remains HuggingFace; this "
            f"mirror exists so per-sample scorer runs can reference dataset "
            f"examples in the LangSmith Experiments view. Published by "
            f"evals/scripts/build_swe_bench_verified_dataset.py."
        ),
    )

    # LangSmith caps a single create_examples call; chunk to be safe.
    chunk = 100
    total = 0
    for start in range(0, len(examples), chunk):
        batch = examples[start : start + chunk]
        client.create_examples(
            dataset_id=ds.id,
            inputs=[ex["inputs"] for ex in batch],
            outputs=[ex["outputs"] for ex in batch],
            metadata=[ex["metadata"] for ex in batch],
        )
        total += len(batch)
        print(f"  uploaded {total}/{len(examples)}", file=sys.stderr)

    print(
        f"[done] published {total} examples to LangSmith dataset {DATASET_NAME} ({ds.id})",
        file=sys.stderr,
    )
    return str(ds.id)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build_swe_bench_verified_dataset")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Delete the existing LangSmith dataset (if any) and republish from scratch.",
    )
    args = ap.parse_args(argv)
    examples = _load_examples()
    ds_id = _publish(examples, force=args.force)
    print(
        json.dumps(
            {
                "dataset_id": ds_id,
                "dataset_name": DATASET_NAME,
                "sample_count": len(examples),
                "hf_revision": _hf_revision(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

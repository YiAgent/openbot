"""Mirror SWT-Bench Verified to a LangSmith dataset — PRD §3.1.

SWT-Bench Verified (NeurIPS 2024, ``logic-star-ai/swt-bench``) is the
sister benchmark to SWE-bench Verified. Same 500-instance pool, filtered
down to the 433 instances where the bug fix is *test-validatable* —
i.e., where the gold ``test_patch`` actually distinguishes the buggy code
from the fix. The model's job is to produce a regression *test* that
fails on the bug and passes once the fix is applied.

We mirror the official BM25-augmented Verified dataset published by ETH
SRI (``eth-sri/SWT-bench_Verified_bm25_27k_zsb``). Same rationale as the
SWE-bench Verified mirror: Inspect AI pulls the dataset directly at
runtime, but per-sample scorer runs need a LangSmith example to reference
in the Experiments tab.

Idempotent: aborts if the dataset already exists, unless ``--force`` is
passed (mirrors ``build_swe_bench_verified_dataset.py``).

Usage::

    doppler run --project openbot --config dev -- \\
        uv run python -m evals.scripts.build_swt_bench_verified_dataset
"""

from __future__ import annotations

import argparse
import json
import sys

DATASET_NAME = "test_swt_bench_verified"
HF_DATASET = "eth-sri/SWT-bench_Verified_bm25_27k_zsb"
HF_SPLIT = "test"


def _load_examples() -> tuple[list[dict], str]:
    """Pull the dataset from HF and shape it for LangSmith.

    Each LangSmith example shape:
      - inputs:  agent-facing surface — issue text + repo + base_commit.
                 ``text`` is the official BM25-augmented prompt; surfaced
                 so future solvers can A/B with/without retrieved context.
      - outputs: gold *test* patch (what we're effectively asking the
                 model to generate).
      - metadata: everything else the SWT scorer needs (gold *code* patch,
                  FAIL_TO_PASS, PASS_TO_PASS, version, environment_setup_commit,
                  hints_text, instance_id, BM25 hits) + provenance fields.

    Returns ``(examples, hf_revision)`` so the publish step can bake the
    revision into the dataset description.
    """
    from datasets import load_dataset

    print(f"[hf] loading {HF_DATASET} ({HF_SPLIT})", file=sys.stderr)
    ds = load_dataset(HF_DATASET, split=HF_SPLIT)
    # Best-effort revision capture. Older HF clients expose ``download_checksums``
    # only when ``info.download_checksums`` is populated; otherwise we fall
    # back to ``info.dataset_info.version`` or "unknown".
    info = getattr(ds, "info", None)
    revision = "unknown"
    if info is not None:
        revision = getattr(info, "version", None) or "unknown"
        if revision == "unknown":
            ck = getattr(info, "download_checksums", None) or {}
            if ck:
                first = next(iter(ck.values()))
                revision = first.get("checksum", "unknown") or "unknown"
    revision = str(revision)[:40] if revision != "unknown" else "unknown"

    examples: list[dict] = []
    for row in ds:
        instance_id = str(row["instance_id"])
        examples.append(
            {
                "inputs": {
                    "instance_id": instance_id,
                    "problem_statement": row["problem_statement"],
                    "repo": row["repo"],
                    "base_commit": row["base_commit"],
                    "version": row["version"],
                    # BM25-augmented prompt from the eth-sri release; not fed
                    # to the model yet but kept here so solvers can opt in.
                    "text": row.get("text", ""),
                },
                "outputs": {
                    # Gold test patch — the regression test the model is
                    # being asked to (re)produce. Scorer doesn't use this
                    # directly; it just runs the model's tests with the
                    # gold *code* patch toggled on/off.
                    "gold_test_patch": row["test_patch"],
                },
                "metadata": {
                    "instance_id": instance_id,
                    "dataset_version": DATASET_NAME,
                    "hf_dataset": HF_DATASET,
                    "hf_split": HF_SPLIT,
                    "hf_revision": revision,
                    # Gold *code* fix — scorer applies this on top of the
                    # model's test patch and re-runs F2P to validate.
                    "gold_code_patch": row["patch"],
                    "FAIL_TO_PASS": row["FAIL_TO_PASS"],
                    "PASS_TO_PASS": row["PASS_TO_PASS"],
                    "environment_setup_commit": row["environment_setup_commit"],
                    "hints_text": row.get("hints_text", ""),
                    "created_at": row.get("created_at", ""),
                    "difficulty": row.get("difficulty", ""),
                    "hits": row.get("hits", []),
                },
            }
        )
    print(f"[hf] loaded {len(examples)} examples (revision={revision[:12]})", file=sys.stderr)
    return examples, revision


def _publish(examples: list[dict], revision: str, *, force: bool) -> str:
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

    ds = client.create_dataset(
        dataset_name=DATASET_NAME,
        description=(
            f"SWT-Bench Verified mirror — {HF_DATASET} ({HF_SPLIT} split), "
            f"HF revision {revision[:12]}. Sister benchmark to SWE-bench Verified "
            f"(same instance_ids, same Epoch Docker images). Source of truth "
            f"remains HuggingFace; this mirror exists so per-sample scorer runs "
            f"can reference dataset examples in the LangSmith Experiments view. "
            f"Published by evals/scripts/build_swt_bench_verified_dataset.py."
        ),
    )

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
    ap = argparse.ArgumentParser(prog="build_swt_bench_verified_dataset")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Delete the existing LangSmith dataset (if any) and republish from scratch.",
    )
    args = ap.parse_args(argv)
    examples, revision = _load_examples()
    ds_id = _publish(examples, revision, force=args.force)
    print(
        json.dumps(
            {
                "dataset_id": ds_id,
                "dataset_name": DATASET_NAME,
                "sample_count": len(examples),
                "hf_revision": revision,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

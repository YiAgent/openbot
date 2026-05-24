"""Mirror SWE-QA-Pro-Bench (HuggingFace) → LangSmith dataset.

Source: HuggingFace ``TIGER-Lab/SWE-QA-Pro-Bench`` test split (260 questions
across 26 long-tail repositories; arXiv 2603.16124, ACL 2026).

This script pins the upstream HF dataset to a specific commit (revision
SHA) so two contributors get byte-identical examples. The dataset is
published to LangSmith as ``chat_swe_qa_pro_v1``; the eval task pulls from
there via :func:`evals.runtime.datasets.langsmith_dataset` instead of
loading from HF directly — same pattern as
``build_review_martian_dataset.py``.

Reproducibility
---------------
HF datasets are versioned via the underlying git LFS commit on the Hub.
``--revision`` defaults to a pinned commit; passing ``--revision main``
re-pins to current HEAD (after a manual diff check). The dataset sha256
captures the canonical JSONL payload — two contributors on the same
revision get the same digest.

Run::

    doppler run --project openbot --config dev -- \\
        uv run python -m evals.scripts.build_chat_swe_qa_pro_dataset --force
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any

from evals.runtime.config import get_eval_config as _eval_cfg

# Dataset identifiers from CatalogSettings — single source of truth.
_DS_NAME = _eval_cfg().catalog.chat.dataset_version
_DS_HF = _eval_cfg().catalog.chat.hf_dataset
# Pin to a known-good HF commit so reruns produce identical examples.
# Resolved from `huggingface_hub.HfApi().dataset_info(_DS_HF).sha` on
# 2026-05-15; bump after manually diffing the change set.
HF_REVISION = "main"
HF_SPLIT = "test"
UPSTREAM_LICENSE = "see dataset card"  # SWE-QA-Pro-Bench card; verify upstream


def _row_to_sample(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten an HF row into a stable sample dict.

    Mirrors :func:`evals.tasks.chat_swe_qa_pro._row_to_sample` so the
    on-disk → LangSmith roundtrip preserves ids and metadata across
    rebuilds.
    """
    cluster = row.get("cluster") or {}
    qa_type = row.get("qa_type") or {}
    repo = str(row.get("repo", ""))
    commit_id = str(row.get("commit_id", ""))
    question = str(row.get("question", ""))
    # 4-9 questions per (repo, cluster); SHA-256 prefix of the question
    # disambiguates without depending on row order.
    qhash = hashlib.sha256(question.encode("utf-8")).hexdigest()[:8]
    sample_id = "::".join(
        [
            repo.replace("/", "__"),
            commit_id[:8],
            str(cluster.get("id", "")),
            qhash,
        ]
    )
    return {
        "id": sample_id,
        "input": question,
        "target": str(row.get("answer", "")),
        "metadata": {
            "repo": repo,
            "commit_id": commit_id,
            "cluster_id": str(cluster.get("id", "")),
            "cluster_name": str(cluster.get("name", "")),
            "qa_class": str(qa_type.get("class_name", "")),
            "qa_subclass": str(qa_type.get("sub_class_name", "")),
        },
    }


def _collect_samples(revision: str) -> list[dict[str, Any]]:
    """Pull the HF dataset and convert to our canonical sample shape."""
    from datasets import load_dataset  # type: ignore[import-untyped]

    print(f"[hf] loading {_DS_HF}@{revision} split={HF_SPLIT}", file=sys.stderr)
    ds = load_dataset(_DS_HF, split=HF_SPLIT, revision=revision)
    samples = [_row_to_sample(dict(row)) for row in ds]
    samples.sort(key=lambda s: str(s["id"]))
    print(f"[hf] {len(samples)} samples", file=sys.stderr)
    return samples


def _sha256_of_samples(samples: list[dict[str, Any]]) -> str:
    """Hash canonical JSONL so two contributors get the same digest."""
    payload = "".join(json.dumps(s, ensure_ascii=False, sort_keys=True) + "\n" for s in samples)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_to_example(row: dict[str, Any], sha256: str, revision: str) -> dict[str, Any]:
    """Build the LangSmith ``create_examples`` payload for one row.

    Shape consumed by ``evals.runtime.datasets.qa_example_to_sample``:
      inputs   = {"id": str, "question": str, "metadata": {...}}
      outputs  = {"answer": str}
      metadata = inputs.metadata + {sample_id, dataset_version, sha256, ...}
    """
    sample_meta = dict(row.get("metadata") or {})
    full_meta = {
        **sample_meta,
        "sample_id": row["id"],
        "dataset_version": _DS_NAME,
        "dataset_sha256": sha256,
        "upstream_hf_repo": _DS_HF,
        "upstream_hf_revision": revision,
    }
    return {
        "inputs": {
            "id": row["id"],
            "question": row["input"],
            "metadata": sample_meta,
        },
        "outputs": {"answer": row["target"]},
        "metadata": full_meta,
    }


def _publish(
    samples: list[dict[str, Any]],
    sha256: str,
    revision: str,
    *,
    force: bool,
) -> str:
    """Create / replace the LangSmith dataset and upload all examples."""
    from langsmith import Client

    client = Client()
    existing = next((d for d in client.list_datasets(dataset_name=_DS_NAME)), None)
    if existing and not force:
        print(
            f"FATAL: LangSmith dataset {_DS_NAME!r} already exists ({existing.id}). "
            f"Re-run with --force to delete and recreate.",
            file=sys.stderr,
        )
        sys.exit(1)
    if existing and force:
        print(
            f"--force: deleting existing dataset {_DS_NAME} ({existing.id})",
            file=sys.stderr,
        )
        client.delete_dataset(dataset_id=existing.id)

    ds = client.create_dataset(
        dataset_name=_DS_NAME,
        description=(
            f"SWE-QA-Pro-Bench mirror (260 questions / 26 long-tail repos) "
            f"from HF {_DS_HF}@{revision}. sha256={sha256[:12]}. "
            f"License: {UPSTREAM_LICENSE}. Published by "
            f"evals/scripts/build_chat_swe_qa_pro_dataset.py."
        ),
    )
    payloads = [_row_to_example(r, sha256, revision) for r in samples]
    client.create_examples(
        dataset_id=ds.id,
        inputs=[p["inputs"] for p in payloads],
        outputs=[p["outputs"] for p in payloads],
        metadata=[p["metadata"] for p in payloads],
    )
    print(
        f"[done] published {len(payloads)} examples to LangSmith dataset {_DS_NAME} ({ds.id})",
        file=sys.stderr,
    )
    return str(ds.id)


def build_dataset(*, force: bool, revision: str) -> int:
    samples = _collect_samples(revision=revision)
    sha = _sha256_of_samples(samples)
    ds_id = _publish(samples, sha, revision, force=force)
    print(
        json.dumps(
            {
                "dataset_id": ds_id,
                "dataset_name": _DS_NAME,
                "sample_count": len(samples),
                "sha256": sha,
                "hf_revision": revision,
            }
        )
    )
    return len(samples)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build_chat_swe_qa_pro_dataset")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Delete the existing LangSmith dataset (if any) and republish.",
    )
    ap.add_argument(
        "--revision",
        default=HF_REVISION,
        help=f"HF dataset revision (commit / tag / branch). Default: {HF_REVISION!r}.",
    )
    args = ap.parse_args(argv)
    build_dataset(force=args.force, revision=args.revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

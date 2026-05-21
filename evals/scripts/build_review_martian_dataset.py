"""Build the Martian offline benchmark and publish it as a LangSmith dataset.

PRD §4.1 / §7.3 — 50 PRs across 5 repos from upstream
``withmartian/code-review-benchmark`` (pinned to ``UPSTREAM_COMMIT``).

Source of truth is **LangSmith**: this script pins the upstream commit,
downloads each PR's golden comments + ``.diff``, then calls
``client.create_dataset`` + ``client.create_examples`` so the dataset
lives in LangSmith. No local JSONL is written. Inspect tasks pull from
LangSmith via ``evals.common.datasets.langsmith_dataset``.

The local manifest YAML is the only thing kept on disk — it carries the
upstream commit, sha256 of the example payload, and license metadata.
(Trace routing is no longer per-dataset; all eval cells now share the
single ``LANGSMITH_PROJECT_EVAL`` project — see
``evals/agents/langsmith.py``.)

Reproducibility
---------------
Two contributors running this script on the same ``UPSTREAM_COMMIT`` will
push identical example bodies; the manifest's sha256 captures that
invariant. ``--force`` deletes the LangSmith dataset and recreates from
scratch; default behavior is "abort if it already exists" so an accidental
re-run never silently mutates the published set.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys

import httpx

from evals.common.config import get_eval_config as _eval_cfg

UPSTREAM_REPO = "withmartian/code-review-benchmark"
UPSTREAM_COMMIT = "807d46980c3390efbe8324c0b7a05fe3aa60c455"  # 2026-05-01
UPSTREAM_LICENSE = "MIT"

MARTIAN_REPOS: tuple[str, ...] = (
    "cal_dot_com",
    "discourse",
    "grafana",
    "keycloak",
    "sentry",
)

GOLDEN_RAW_BASE = (
    f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/{UPSTREAM_COMMIT}/offline/golden_comments"
)

_DS_NAME = _eval_cfg().catalog.review.dataset_version

_PR_URL_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def _gh_token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    out = subprocess.run(["gh", "auth", "token"], check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _pr_api_url(pr_url: str) -> str:
    match = _PR_URL_RE.match(pr_url)
    if not match:
        raise ValueError(f"unrecognized PR URL: {pr_url}")
    owner, repo, number = match.groups()
    return f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"


async def _download_diff(client: httpx.AsyncClient, pr_url: str) -> str:
    resp = await client.get(
        _pr_api_url(pr_url),
        headers={
            "Accept": "application/vnd.github.v3.diff",
            "Authorization": f"Bearer {_gh_token()}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


async def _fetch_pr_base_sha(client: httpx.AsyncClient, pr_url: str) -> str:
    """Return the PR's actual base commit SHA on the target repo.

    Previously the dataset stored ``upstream_commit`` (the ``martian-CRB``
    repo's own commit hash) and the solver mis-used it as the target
    repo's base SHA. That guaranteed ``git fetch <sha>`` failed for every
    sample, triggering the ``_used_sha_fallback`` HEAD-checkout path —
    putting the agent into a wrong commit and forcing diff-only review.
    Here we go to the GitHub PR API directly and pull ``base.sha`` so the
    agent sandbox checks out the exact commit the PR was opened against.
    """
    resp = await client.get(
        _pr_api_url(pr_url),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {_gh_token()}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        follow_redirects=True,
    )
    resp.raise_for_status()
    payload = resp.json()
    base = payload.get("base") or {}
    sha = base.get("sha")
    if not isinstance(sha, str) or len(sha) != 40:
        raise ValueError(f"missing/invalid base.sha for {pr_url}: {sha!r}")
    return sha


async def _fetch_repo_golden(client: httpx.AsyncClient, repo: str) -> list[dict]:
    json_url = f"{GOLDEN_RAW_BASE}/{repo}.json"
    resp = await client.get(json_url)
    resp.raise_for_status()
    return resp.json()


def _comments_to_findings(comments: list[dict]) -> list[dict]:
    """Martian gold comments do not provide file/line — leave them empty."""
    return [
        {
            "file": "",
            "line": None,
            "body": c["comment"],
            "severity": c["severity"].lower(),
        }
        for c in comments
    ]


async def _collect_samples() -> list[dict]:
    samples: list[dict] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for repo in MARTIAN_REPOS:
            print(f"[golden] fetching {repo} at {UPSTREAM_COMMIT[:8]}", file=sys.stderr)
            pr_list = await _fetch_repo_golden(client, repo)
            for i, pr in enumerate(pr_list, start=1):
                pr_url = pr["url"]
                print(f"  [diff] {pr_url}", file=sys.stderr)
                diff_text = await _download_diff(client, pr_url)
                # Fetch PR metadata for ``base.sha``: the actual commit the
                # PR was opened against, which the sandbox needs to check
                # out so the agent's ``read_file`` returns the same code
                # the diff was generated against. See ``_fetch_pr_base_sha``
                # docstring for why ``upstream_commit`` (martian-CRB's own
                # SHA) was not a valid substitute.
                base_sha = await _fetch_pr_base_sha(client, pr_url)
                samples.append(
                    {
                        "id": f"martian-{repo}-{i:03d}",
                        "input": diff_text,
                        "target": _comments_to_findings(pr.get("comments", [])),
                        "metadata": {
                            "repo": repo,
                            "pr_url": pr_url,
                            "pr_title": pr.get("pr_title", ""),
                            "base_sha": base_sha,
                            # ``upstream_commit`` is kept for provenance —
                            # it identifies the martian-CRB snapshot the
                            # golden comments came from. Solvers MUST NOT
                            # use it as the target repo's base SHA.
                            "upstream_commit": UPSTREAM_COMMIT,
                        },
                    }
                )
    return samples


def _sha256_of_samples(samples: list[dict]) -> str:
    """Hash the canonical JSONL form so two contributors get the same digest."""
    payload = "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in samples)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_to_example(row: dict, sha256: str) -> dict:
    """Build the LangSmith create_examples payload for one row."""
    metadata = dict(row.get("metadata") or {})
    metadata.update(
        {
            "sample_id": row["id"],
            "dataset_version": _DS_NAME,
            "dataset_sha256": sha256,
        }
    )
    return {
        "inputs": {
            "id": row["id"],
            "diff": row["input"],
            "metadata": row.get("metadata", {}),
        },
        "outputs": {"golden_findings": row["target"]},
        "metadata": metadata,
    }


def _publish(samples: list[dict], sha256: str, *, force: bool) -> str:
    """Create / replace the LangSmith dataset and upload all examples."""
    # Lazy import keeps `--help` working without a langsmith install in PATH.
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
            f"Martian code-review benchmark (50 PRs from 5 repos), pinned to "
            f"upstream commit {UPSTREAM_COMMIT[:12]}. sha256={sha256[:12]}. "
            f"Published by evals/scripts/build_review_martian_dataset.py."
        ),
    )
    payloads = [_row_to_example(r, sha256) for r in samples]
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


async def build_dataset(*, force: bool) -> int:
    samples = await _collect_samples()
    sha = _sha256_of_samples(samples)
    ds_id = _publish(samples, sha, force=force)
    print(json.dumps({"dataset_id": ds_id, "sample_count": len(samples), "sha256": sha}))
    return len(samples)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build_review_martian_dataset")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Delete the existing LangSmith dataset (if any) and republish from scratch.",
    )
    args = ap.parse_args(argv)
    asyncio.run(build_dataset(force=args.force))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

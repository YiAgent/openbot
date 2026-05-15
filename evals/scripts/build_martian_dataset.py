"""Build the locked Martian offline benchmark JSONL.

PRD §4.1 / §7.3 — Martian 50 PR across 5 repos.

This script pins the upstream `withmartian/code-review-benchmark` commit
SHA, downloads its `offline/golden_comments/*.json`, follows each PR URL to
fetch the `.diff` text, and emits a deterministic JSONL alongside a manifest
with the SHA256 of the produced file.

The same commit SHA must be reproduced by anyone running
`uv run python -m evals.scripts.build_martian_dataset` — that is E2-T01's
acceptance criterion ("任意人 inspect eval review_martian 都能拉到同一份 50 PR").

Network use:
  - GitHub Contents API for the 5 golden_comments JSON files (pinned by SHA)
  - github.com/<fork>/pull/<n>.diff for each PR diff (the forks are static)
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import pathlib
import re
import subprocess

import httpx

# Upstream lock — bump this only when the manifest filename also bumps.
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

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DATASET_DIR = REPO_ROOT / "evals" / "datasets"
MANIFEST_DIR = DATASET_DIR / "manifests"

DATASET_NAME = "martian_2026w20"
DATASET_PATH = DATASET_DIR / f"{DATASET_NAME}.jsonl"
DATASET_GZ_PATH = DATASET_DIR / f"{DATASET_NAME}.jsonl.gz"
MANIFEST_PATH = MANIFEST_DIR / f"{DATASET_NAME}.yaml"


_PR_URL_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def _gh_token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    out = subprocess.run(["gh", "auth", "token"], check=True, capture_output=True, text=True)
    return out.stdout.strip()


async def _download_diff(client: httpx.AsyncClient, pr_url: str) -> str:
    match = _PR_URL_RE.match(pr_url)
    if not match:
        raise ValueError(f"unrecognized PR URL: {pr_url}")
    owner, repo, number = match.groups()
    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
    resp = await client.get(
        api_url,
        headers={
            "Accept": "application/vnd.github.v3.diff",
            "Authorization": f"Bearer {_gh_token()}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


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


async def build_dataset() -> int:
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

    samples: list[dict] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for repo in MARTIAN_REPOS:
            print(f"[golden] fetching {repo} at {UPSTREAM_COMMIT[:8]}")
            pr_list = await _fetch_repo_golden(client, repo)
            for i, pr in enumerate(pr_list, start=1):
                pr_url = pr["url"]
                print(f"  [diff] {pr_url}")
                diff_text = await _download_diff(client, pr_url)
                samples.append(
                    {
                        "id": f"martian-{repo}-{i:03d}",
                        "input": diff_text,
                        "target": _comments_to_findings(pr.get("comments", [])),
                        "metadata": {
                            "repo": repo,
                            "pr_url": pr_url,
                            "pr_title": pr.get("pr_title", ""),
                            "upstream_commit": UPSTREAM_COMMIT,
                        },
                    }
                )

    payload = "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in samples)
    DATASET_PATH.write_text(payload, encoding="utf-8")
    # Gzip is the committed form (the raw JSONL exceeds the 1 MB pre-commit
    # large-files threshold). The .jsonl is regenerable from the .gz at any
    # time via evals.tasks.review_martian._ensure_decompressed.
    DATASET_GZ_PATH.write_bytes(gzip.compress(payload.encode("utf-8"), compresslevel=9, mtime=0))
    sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    manifest = (
        f"# evals/datasets/manifests/{DATASET_NAME}.yaml\n"
        f"#\n"
        f"# Real Martian Code Review Benchmark (offline 50 PR set), pinned\n"
        f"# to upstream commit {UPSTREAM_COMMIT[:12]}. Replaces the v0.1 smoke\n"
        f"# fixture for the E2 baseline run (E2-T02).\n"
        f"#\n"
        f"name: {DATASET_NAME}\n"
        f"version: 1\n"
        f'created_at: "2026-05-15"\n'
        f'source: "https://github.com/{UPSTREAM_REPO}"\n'
        f'upstream_commit: "{UPSTREAM_COMMIT}"\n'
        f'sampling_rule: "All 50 PRs from cal.com / discourse / grafana / keycloak / sentry"\n'
        f"sample_count: {len(samples)}\n"
        f'sha256: "{sha256}"\n'
        f'golden_generation: "upstream human-verified golden comments"\n'
        f"public: true\n"
        f'license: "{UPSTREAM_LICENSE} (upstream withmartian/code-review-benchmark)"\n'
        f"related_suites: [review_martian]\n"
        f"deprecated: false\n"
        f"notes: |\n"
        f"  Reproduce with:\n"
        f"      uv run python -m evals.scripts.build_martian_dataset\n"
        f"\n"
        f"  Upstream gold comments do not specify file/line, so each finding's\n"
        f"  ``file`` is empty and ``line`` is null. The LLM judge in\n"
        f"  ``evals.scorers.review_overlap`` is the intended matcher; the\n"
        f"  heuristic judge used by ``review_martian_smoke`` will under-count\n"
        f"  on this dataset by design.\n"
    )
    MANIFEST_PATH.write_text(manifest, encoding="utf-8")
    print(f"[done] wrote {DATASET_PATH} ({len(samples)} samples)")
    print(f"[done] wrote {MANIFEST_PATH}")
    print(f"[done] sha256 {sha256}")
    return len(samples)


if __name__ == "__main__":
    asyncio.run(build_dataset())

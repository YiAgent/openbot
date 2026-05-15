"""E2-T01 — pin the Martian dataset manifest.

The Martian benchmark is the public reproducibility anchor for the v0.1
review baseline. If anything changes the file contents — sample count,
sha256, upstream commit — that is a deliberate dataset bump and must be
acknowledged by updating this test alongside the manifest.

Acceptance criterion (task-list.md → E2-T01):
    任意人 `inspect eval review_martian` 都能拉到同一份 50 PR
"""

from __future__ import annotations

import gzip
import hashlib
import json
import pathlib

import pytest
import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_DATASET = _REPO_ROOT / "evals" / "datasets" / "martian_2026w20.jsonl"
_DATASET_GZ = _REPO_ROOT / "evals" / "datasets" / "martian_2026w20.jsonl.gz"
_MANIFEST = _REPO_ROOT / "evals" / "datasets" / "manifests" / "martian_2026w20.yaml"


def _read_dataset_bytes() -> bytes | None:
    """Return the JSONL bytes from the gzipped form (canonical committed form).

    Falls back to the plain .jsonl when only that exists (e.g. just-after-build).
    """
    if _DATASET_GZ.exists():
        return gzip.decompress(_DATASET_GZ.read_bytes())
    if _DATASET.exists():
        return _DATASET.read_bytes()
    return None


_EXPECTED_SHA256 = "4b432111fbbe854a8c56fcd403e06681dd4bf9d3a5b32a04be8358f1c7573709"
_EXPECTED_UPSTREAM_COMMIT = "807d46980c3390efbe8324c0b7a05fe3aa60c455"
_EXPECTED_SAMPLE_COUNT = 50
_EXPECTED_REPOS = {"cal_dot_com", "discourse", "grafana", "keycloak", "sentry"}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return yaml.safe_load(_MANIFEST.read_text(encoding="utf-8"))


def test_manifest_pins_upstream_commit(manifest: dict) -> None:
    assert manifest["upstream_commit"] == _EXPECTED_UPSTREAM_COMMIT
    assert manifest["source"].endswith("withmartian/code-review-benchmark")


def test_manifest_pins_sha256_and_count(manifest: dict) -> None:
    assert manifest["sha256"] == _EXPECTED_SHA256
    assert manifest["sample_count"] == _EXPECTED_SAMPLE_COUNT


def test_dataset_file_matches_manifest_sha256() -> None:
    payload = _read_dataset_bytes()
    if payload is None:
        pytest.skip(
            "dataset not built locally; run `uv run python -m evals.scripts.build_martian_dataset`"
        )
    actual = hashlib.sha256(payload).hexdigest()
    assert actual == _EXPECTED_SHA256


def test_dataset_rows_cover_all_repos() -> None:
    payload = _read_dataset_bytes()
    if payload is None:
        pytest.skip("dataset not built locally")
    records = [json.loads(line) for line in payload.decode("utf-8").splitlines() if line]
    assert len(records) == _EXPECTED_SAMPLE_COUNT
    repos = {r["metadata"]["repo"] for r in records}
    assert repos == _EXPECTED_REPOS
    for r in records:
        assert r["metadata"]["upstream_commit"] == _EXPECTED_UPSTREAM_COMMIT
        assert "diff --git" in r["input"]

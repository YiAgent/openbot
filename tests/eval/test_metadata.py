"""Unit tests for evals.common.metadata.collect_run_metadata — PRD §10.1."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from evals.common import metadata
from evals.common._metadata_spec import REQUIRED_RUN_FIELD_NAMES

_FAKE_SHA = "1234567890abcdef1234567890abcdef12345678"


@pytest.fixture
def _patched_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub `git rev-parse HEAD` to a deterministic SHA (no real subprocess)."""

    def _fake_run(*_args: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout=f"{_FAKE_SHA}\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)


def _common_kwargs() -> dict[str, str]:
    return {
        "dataset_version": "martian_review_v1",
        "dataset_sha256": "0" * 64,
    }


def test_collect_returns_all_required_fields(_patched_git: None) -> None:
    md = metadata.collect_run_metadata("review_martian", "smoke", **_common_kwargs())
    assert md.keys() >= REQUIRED_RUN_FIELD_NAMES


def test_collect_propagates_positional_args(_patched_git: None) -> None:
    md = metadata.collect_run_metadata("triage_internal", "regression", **_common_kwargs())
    assert md["suite_name"] == "triage_internal"
    assert md["mode"] == "regression"


def test_git_sha_comes_from_subprocess(_patched_git: None) -> None:
    md = metadata.collect_run_metadata("review_martian", "smoke", **_common_kwargs())
    assert md["git_sha"] == _FAKE_SHA


def test_runner_version_is_inspect_ai_version(_patched_git: None) -> None:
    """`inspect_ai.__version__` is the live source; assert it parses as semver-ish."""
    md = metadata.collect_run_metadata("review_martian", "smoke", **_common_kwargs())
    assert md["runner_version"]  # non-empty
    assert md["runner_version"].count(".") >= 1  # x.y or x.y.z


def test_prompt_and_workflow_versions_fallback_to_unknown(_patched_git: None) -> None:
    """openbot.prompts / openbot.workflows don't exist in the v0.1 skeleton yet."""
    md = metadata.collect_run_metadata("review_martian", "smoke", **_common_kwargs())
    # When the modules DO ship, this test will fail loudly — that's intentional;
    # it forces a sync with PRD §10.1 contract semantics.
    assert md["prompt_version"] == "unknown"
    assert md["workflow_version"] == "unknown"


def test_judge_prompt_version_fallback_zero(_patched_git: None) -> None:
    md = metadata.collect_run_metadata("review_martian", "smoke", **_common_kwargs())
    # E1-T05 ships JUDGE_PROMPT_VERSION; until then fallback is 0.
    assert md["judge_prompt_version"] == 0


def test_triggered_by_local_by_default(_patched_git: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("OPENBOT_EVAL_TRIGGER", raising=False)
    md = metadata.collect_run_metadata("review_martian", "smoke", **_common_kwargs())
    assert md["triggered_by"] == "local"


def test_triggered_by_github_actor_when_in_actions(
    _patched_git: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    md = metadata.collect_run_metadata("review_martian", "smoke", **_common_kwargs())
    assert md["triggered_by"] == "github_actor"


def test_triggered_by_cron_via_explicit_env(
    _patched_git: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("OPENBOT_EVAL_TRIGGER", "cron")
    md = metadata.collect_run_metadata("review_martian", "smoke", **_common_kwargs())
    assert md["triggered_by"] == "cron"


def test_started_at_is_iso_utc(_patched_git: None) -> None:
    md = metadata.collect_run_metadata("review_martian", "smoke", **_common_kwargs())
    # `datetime.fromisoformat` will raise on a malformed string.
    from datetime import datetime

    dt = datetime.fromisoformat(md["started_at"])
    assert dt.utcoffset() is not None  # tz-aware


def test_collect_runs_regardless_of_cwd(
    _patched_git: None, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC line: '任意 cwd 调一次能产出完整 dict'."""
    monkeypatch.chdir(tmp_path)
    md = metadata.collect_run_metadata("review_martian", "smoke", **_common_kwargs())
    assert md.keys() >= REQUIRED_RUN_FIELD_NAMES


def test_metadata_passes_log_run_metadata_guard(_patched_git: None) -> None:
    """Output of collect_* must satisfy log_run_metadata without raising."""
    from unittest.mock import MagicMock

    from evals.common import langsmith as ls

    md = metadata.collect_run_metadata("review_martian", "smoke", **_common_kwargs())
    client = MagicMock()
    ls.log_run_metadata(client, "run-1", md)  # would raise on missing field
    client.update_run.assert_called_once()

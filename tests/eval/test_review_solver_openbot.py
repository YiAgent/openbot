"""Tests for the rewritten evals/solvers/review.py (openbot.evaluation path).

These replace the old internal-implementation tests with tests on
the observable behavior of the new thin wrapper.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from evals.solvers.review import (
    _domain_findings_to_eval,
    openbot_review_solver,
)
from openbot.domain.review import Finding as DomainFinding
from openbot.domain.review import ReviewFindings

# domain findings conversion


def test_domain_findings_to_eval_maps_message_to_body() -> None:
    """domain.Finding.message maps to solver Finding.body."""
    df = DomainFinding(severity="high", file="a.py", message="null pointer", line=42)
    result = _domain_findings_to_eval((df,))
    assert len(result) == 1
    assert result[0]["body"] == "null pointer"
    assert result[0]["file"] == "a.py"
    assert result[0]["line"] == 42
    assert result[0]["severity"] == "high"


def test_domain_findings_to_eval_maps_nit_to_low() -> None:
    """Domain nit severity maps to low (scorers do not know nit)."""
    df = DomainFinding(severity="nit", file="b.py", message="style")
    result = _domain_findings_to_eval((df,))
    assert result[0]["severity"] == "low"


def test_domain_findings_to_eval_empty() -> None:
    result = _domain_findings_to_eval(())
    assert result == []


# openbot_review_solver


@pytest.mark.asyncio
async def test_solver_stores_findings_in_metadata(monkeypatch) -> None:
    """Solver calls run_review_sample and stores findings in state.metadata."""
    expected_findings = ReviewFindings(
        summary="two issues",
        findings=(
            DomainFinding(severity="high", file="x.py", message="bug", line=5),
            DomainFinding(severity="medium", file="y.py", message="smell"),
        ),
    )
    monkeypatch.setattr(
        "evals.solvers.review.run_review_sample",
        AsyncMock(return_value=expected_findings),
    )

    state = MagicMock()
    state.input_text = "some diff"
    state.metadata = {"repo": "org/repo", "pr_number": 7}
    state.sample_id = "sample-1"

    solver_fn = openbot_review_solver()
    result_state = await solver_fn(state, MagicMock())

    findings = result_state.metadata["candidate_findings"]
    assert len(findings) == 2
    assert findings[0]["body"] == "bug"
    assert findings[0]["severity"] == "high"
    assert findings[1]["body"] == "smell"


@pytest.mark.asyncio
async def test_solver_uses_repo_and_pr_number_from_metadata(monkeypatch) -> None:
    """Solver passes repo and pr_number from state.metadata to run_review_sample."""
    captured: dict = {}

    async def fake_run_review(*, repo, pr_number, pr_diff, **kwargs):
        captured["repo"] = repo
        captured["pr_number"] = pr_number
        captured["pr_diff"] = pr_diff
        return ReviewFindings(summary="ok", findings=())

    monkeypatch.setattr("evals.solvers.review.run_review_sample", fake_run_review)

    state = MagicMock()
    state.input_text = "the diff"
    state.metadata = {"repo": "owner/repo", "pr_number": 42}
    state.sample_id = "s1"

    await openbot_review_solver()(state, MagicMock())

    assert captured["repo"] == "owner/repo"
    assert captured["pr_number"] == 42
    assert captured["pr_diff"] == "the diff"


@pytest.mark.asyncio
async def test_solver_stores_findings_json_in_metadata(monkeypatch) -> None:
    """candidate_findings_json is a valid JSON string of findings."""
    monkeypatch.setattr(
        "evals.solvers.review.run_review_sample",
        AsyncMock(
            return_value=ReviewFindings(
                summary="ok",
                findings=(DomainFinding(severity="low", file="f.py", message="minor"),),
            )
        ),
    )

    state = MagicMock()
    state.input_text = ""
    state.metadata = {}
    state.sample_id = "s2"

    await openbot_review_solver()(state, MagicMock())

    raw_json = state.metadata.get("candidate_findings_json")
    assert raw_json is not None
    parsed = json.loads(raw_json)
    assert "findings" in parsed
    assert parsed["findings"][0]["body"] == "minor"


# ── pr_url parsing + file_reader wiring ──────────────────────────────────────


@pytest.mark.asyncio
async def test_solver_parses_github_repo_from_pr_url(monkeypatch) -> None:
    """Solver extracts owner/repo from pr_url and passes it to run_review_sample."""
    captured: dict = {}

    async def fake_run(*, repo, pr_number, pr_diff, **kwargs):
        captured["repo"] = repo
        captured["pr_number"] = pr_number
        return ReviewFindings(summary="ok", findings=())

    monkeypatch.setattr("evals.solvers.review.run_review_sample", fake_run)

    state = MagicMock()
    state.input_text = "diff text"
    state.metadata = {
        "repo": "cal_dot_com",  # alias — should NOT be used
        "pr_url": "https://github.com/calcom/cal.com/pull/8087",
        "base_sha": "ba9688a04a8398c9a8332ee7061bfae2f2efd524",
    }
    state.sample_id = "s3"

    await openbot_review_solver()(state, MagicMock())

    assert captured["repo"] == "calcom/cal.com"
    assert captured["pr_number"] == 8087


@pytest.mark.asyncio
async def test_solver_passes_file_reader_when_pr_url_and_base_sha_present(monkeypatch) -> None:
    """Solver builds a GitHubFileReader and passes it to run_review_sample."""
    from openbot.evaluation.github_file_reader import GitHubFileReader

    captured: dict = {}

    async def fake_run(*, repo, pr_number, pr_diff, file_reader=None, **kwargs):
        captured["file_reader"] = file_reader
        return ReviewFindings(summary="ok", findings=())

    monkeypatch.setattr("evals.solvers.review.run_review_sample", fake_run)

    state = MagicMock()
    state.input_text = "diff"
    state.metadata = {
        "pr_url": "https://github.com/calcom/cal.com/pull/8087",
        "base_sha": "ba9688a04a8398c9a8332ee7061bfae2f2efd524",
    }
    state.sample_id = "s4"

    await openbot_review_solver()(state, MagicMock())

    fr = captured["file_reader"]
    assert isinstance(fr, GitHubFileReader)
    assert fr.repo == "calcom/cal.com"
    assert fr.ref == "ba9688a04a8398c9a8332ee7061bfae2f2efd524"


@pytest.mark.asyncio
async def test_solver_passes_no_file_reader_when_pr_url_missing(monkeypatch) -> None:
    """Solver passes file_reader=None when pr_url is absent from metadata."""
    captured: dict = {}

    async def fake_run(*, repo, pr_number, pr_diff, file_reader=None, **kwargs):
        captured["file_reader"] = file_reader
        return ReviewFindings(summary="ok", findings=())

    monkeypatch.setattr("evals.solvers.review.run_review_sample", fake_run)

    state = MagicMock()
    state.input_text = "diff"
    state.metadata = {"repo": "org/repo", "pr_number": 1}
    state.sample_id = "s5"

    await openbot_review_solver()(state, MagicMock())

    assert captured["file_reader"] is None

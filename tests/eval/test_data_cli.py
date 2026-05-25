"""CLI routing tests for evals/data/__main__.py."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from evals.data.__main__ import main


def test_unknown_suite_exits_nonzero() -> None:
    with (
        patch("evals.data.__main__._resolve", side_effect=SystemExit("unknown suite 'x'")),
        pytest.raises(SystemExit),
    ):
        main(["inspect", "nosuchsuite"])


def test_inspect_cmd_prints_suite_info(capsys: pytest.CaptureFixture[str]) -> None:
    # Wire a minimal DATASETS dict so the CLI can resolve "fake"
    from evals.data._base import EvalDataset

    class _FakeSuite(EvalDataset):
        suite = "fake"
        dataset_version = "fake_v1"
        instance_id_field = "id"

        def collect(self) -> list[Any]:
            return []

        def example_to_sample(self, example: Any) -> Any:
            return None

        def build_task(self, *, solver: Any, scorer: Any = None, extra_metadata: Any = None) -> Any:
            raise NotImplementedError

    fake = _FakeSuite()
    with patch("evals.data.__main__._resolve", return_value=fake):
        rc = main(["inspect", "fake"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "fake" in out
    assert "fake_v1" in out


def test_collect_cmd_outputs_json(capsys: pytest.CaptureFixture[str]) -> None:
    fake_examples = [{"inputs": {"id": "1"}, "outputs": None, "metadata": {}}]

    mock_suite = MagicMock()
    mock_suite.collect.return_value = fake_examples
    mock_suite.suite = "fix"

    with patch("evals.data.__main__._resolve", return_value=mock_suite):
        rc = main(["collect", "fix"])

    assert rc == 0
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert len(parsed) == 1
    assert parsed[0]["inputs"]["id"] == "1"


def test_refresh_cmd_invokes_collect_and_publish(capsys: pytest.CaptureFixture[str]) -> None:
    fake_result = MagicMock()
    fake_result.example_count = 42
    fake_result.sha256 = "abc123"

    mock_suite = MagicMock()
    mock_suite.collect.return_value = []
    mock_suite.publish.return_value = fake_result
    mock_suite.suite = "fix"

    with patch("evals.data.__main__._resolve", return_value=mock_suite):
        rc = main(["refresh", "fix"])

    assert rc == 0
    mock_suite.collect.assert_called_once()
    mock_suite.publish.assert_called_once()
    out = capsys.readouterr().out
    assert "42" in out


def test_writeback_cmd_routes_to_suite(tmp_path: Any) -> None:
    report = tmp_path / "r.json"
    report.write_text('{"resolved":[],"unresolved":[],"error":[]}')

    mock_result = MagicMock()
    mock_suite = MagicMock()
    mock_suite.writeback_grades.return_value = mock_result
    mock_suite.suite = "fix"

    with patch("evals.data.__main__._resolve", return_value=mock_suite):
        rc = main(["writeback", "fix", "--report", str(report), "--experiment", "exp1"])

    assert rc == 0
    mock_suite.writeback_grades.assert_called_once_with(
        report_path=str(report),
        experiment_name="exp1",
        dry_run=False,
    )

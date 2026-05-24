"""Enabled in Task 9 once FIX/SWT singletons land."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from evals.data._writeback import run_writeback

pytestmark = pytest.mark.skip(reason="enabled in Task 9")
# Tests are pre-written; remove the skip marker in Task 9 Step 3.


@pytest.fixture(params=["fix", "swt"])
def graded_suite(request):  # type: ignore[no-untyped-def]
    from evals.data import DATASETS

    return DATASETS[request.param]


def test_writeback_creates_feedback(graded_suite: Any, tmp_path: Any) -> None:
    report = tmp_path / "r.json"
    report.write_text('{"resolved":["a__b-1"],"unresolved":[],"error":[]}')
    feedbacks: list[dict[str, Any]] = []

    class _FakeClient:
        def list_runs(self, **kw: Any):  # type: ignore[no-untyped-def]
            return iter(
                [
                    SimpleNamespace(
                        id="run-1",
                        extra={"metadata": {"instance_id": "a__b-1"}},
                    )
                ]
            )

        def create_feedback(self, **kw: Any) -> None:
            feedbacks.append(kw)

    summary = run_writeback(
        suite=graded_suite,
        report_path=str(report),
        experiment_name="exp",
        dry_run=False,
        client=_FakeClient(),
    )
    assert summary.feedback_written == 1
    assert feedbacks[0]["score"] == 1.0
    assert feedbacks[0]["key"] == graded_suite.feedback_key


def test_writeback_dry_run(tmp_path: Any) -> None:
    from evals.data import FIX

    report = tmp_path / "r.json"
    report.write_text('{"resolved":["x"],"unresolved":[],"error":[]}')

    class _FakeClient:
        def list_runs(self, **kw: Any):  # type: ignore[no-untyped-def]
            return iter([SimpleNamespace(id="r", extra={"metadata": {"instance_id": "x"}})])

        def create_feedback(self, **kw: Any) -> None:
            raise AssertionError("dry_run must not write")

    summary = run_writeback(
        suite=FIX,
        report_path=str(report),
        experiment_name="e",
        dry_run=True,
        client=_FakeClient(),
    )
    assert summary.dry_run is True
    assert summary.feedback_written == 0


@pytest.mark.parametrize("suite_name", ["review", "chat"])
def test_writeback_raises_not_implemented(suite_name: str) -> None:
    from evals.data import DATASETS

    suite = DATASETS[suite_name]
    with pytest.raises(NotImplementedError):
        suite.writeback_grades(report_path="x", experiment_name="e")

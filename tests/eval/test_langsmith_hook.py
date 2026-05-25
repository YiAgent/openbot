"""Contract tests for the LangSmith Experiment anchor hook.

The hook (registered in ``evals.runtime.langsmith_hook``) issues exactly one
LangSmith root Run per scored sample, ``name = instance_id``, anchored to the
matching dataset Example via ``reference_example_id``. That anchor is what
``evals/scripts/writeback_*_grades.py`` keys on after the offline Docker
harness produces pass@1 — without it, writeback can't find the run to attach
feedback to.

These tests exercise the pure helpers (``_build_session``,
``_ExperimentSession.provision``, ``_post_sample``) against an in-memory fake
LangSmith Client. The Inspect AI event-loop integration is one thin layer on
top and isn't worth its own constructor mocks.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from inspect_ai.scorer import Score

from evals.runtime import langsmith_hook
from evals.runtime.langsmith_hook import (
    _build_session,
    _ExperimentSession,
    _LangSmithExperimentHook,
    _post_sample,
)


class _FakeExample:
    def __init__(self, example_id: str, instance_id: str) -> None:
        self.id = example_id
        self.inputs = {"instance_id": instance_id}
        self.metadata: dict[str, Any] = {}


class _FakeDataset:
    def __init__(self, dataset_id: str = "ds-1") -> None:
        self.id = dataset_id


class _FakeProject:
    def __init__(self, project_id: str = "proj-1") -> None:
        self.id = project_id


_DEFAULT_DATASET = _FakeDataset()


class _FakeClient:
    """Minimal LangSmith Client stand-in that records calls."""

    def __init__(
        self,
        *,
        dataset: _FakeDataset | None = _DEFAULT_DATASET,
        examples: list[_FakeExample] | None = None,
    ) -> None:
        self._dataset = dataset
        self._examples = examples or []
        self.created_runs: list[dict[str, Any]] = []
        self.created_feedback: list[dict[str, Any]] = []
        self.created_projects: list[dict[str, Any]] = []

    def list_datasets(self, *, dataset_name: str):
        if self._dataset is None:
            return iter([])
        return iter([self._dataset])

    def create_project(self, **kwargs):
        self.created_projects.append(kwargs)
        return _FakeProject()

    def list_examples(self, *, dataset_id: str):
        return iter(self._examples)

    def create_run(self, **kwargs):
        self.created_runs.append(kwargs)

    def create_feedback(self, **kwargs):
        self.created_feedback.append(kwargs)


# ---------------------------------------------------------------------------
# _build_session — opt-out / metadata propagation
# ---------------------------------------------------------------------------


def test_build_session_returns_none_when_dataset_name_missing() -> None:
    assert _build_session(dataset_name=None, spec_metadata={}) is None
    assert _build_session(dataset_name="", spec_metadata={}) is None


def test_build_session_honors_explicit_optout() -> None:
    result = _build_session(
        dataset_name="ds",
        spec_metadata={"langsmith_experiment": False},
    )
    assert result is None


def test_build_session_propagates_metadata() -> None:
    session = _build_session(
        dataset_name="dataset_v1",
        spec_metadata={
            "solver_family": "openbot_agent",
            "model": "anthropic:claude-x",
            "git_sha": "abc123",
            "judge_label": "martian_crb",
        },
    )
    assert session is not None
    assert session.dataset_name == "dataset_v1"
    assert session.experiment_name.startswith("dataset_v1-openbot_agent-")
    assert session.extra_metadata["solver_family"] == "openbot_agent"
    # ``model:foo`` form gets stripped of the provider prefix for the label.
    assert session.extra_metadata["model"] == "claude-x"
    # Free-form metadata is forwarded unless it conflicts with a control key.
    assert session.extra_metadata["judge_label"] == "martian_crb"


# ---------------------------------------------------------------------------
# _ExperimentSession.provision — happy path + missing-dataset noop
# ---------------------------------------------------------------------------


def _make_session(**overrides: Any) -> _ExperimentSession:
    defaults: dict[str, Any] = {
        "dataset_name": "ds",
        "experiment_name": "ds-solver-202000",
        "instance_id_field": "instance_id",
        "extra_metadata": {"solver_family": "openbot_agent"},
    }
    defaults.update(overrides)
    return _ExperimentSession(**defaults)


def test_provision_indexes_examples_and_creates_project(monkeypatch) -> None:
    fake = _FakeClient(
        examples=[
            _FakeExample("ex-1", "instA"),
            _FakeExample("ex-2", "instB"),
        ]
    )
    monkeypatch.setattr("langsmith.Client", lambda: fake)

    session = _make_session()
    session.provision()

    assert session.usable is True
    assert session.project_id == "proj-1"
    assert session.example_index == {"instA": "ex-1", "instB": "ex-2"}
    assert fake.created_projects[0]["project_name"] == "ds-solver-202000"
    assert fake.created_projects[0]["reference_dataset_id"] == "ds-1"


def test_provision_missing_dataset_marks_session_inert(monkeypatch) -> None:
    fake = _FakeClient(dataset=None)
    monkeypatch.setattr("langsmith.Client", lambda: fake)

    session = _make_session()
    session.provision()

    assert session.usable is False
    assert session.project_id is None
    assert fake.created_projects == []


def test_provision_swallows_exceptions(monkeypatch, caplog) -> None:
    def _boom():
        raise RuntimeError("network down")

    monkeypatch.setattr("langsmith.Client", _boom)

    session = _make_session()
    with caplog.at_level(logging.WARNING):
        session.provision()

    assert session.usable is False
    assert any("provisioning failed" in rec.message for rec in caplog.records)


def test_provision_is_idempotent(monkeypatch) -> None:
    """Calling provision a second time must not double-create the project."""
    calls: list[str] = []

    class _CountingClient(_FakeClient):
        def create_project(self, **kwargs):
            calls.append("create_project")
            return super().create_project(**kwargs)

    fake = _CountingClient(examples=[_FakeExample("ex-1", "instA")])
    monkeypatch.setattr("langsmith.Client", lambda: fake)

    session = _make_session()
    session.provision()
    session.provision()
    assert calls == ["create_project"]


# ---------------------------------------------------------------------------
# _post_sample — root Run keyed by instance_id; per-scorer feedback fan-out
# ---------------------------------------------------------------------------


def _stub_feedback_config(monkeypatch) -> None:
    monkeypatch.setattr(
        langsmith_hook,
        "ensure_feedback_config",
        lambda *_args, **_kwargs: None,
    )


def _provisioned_session() -> _ExperimentSession:
    session = _make_session()
    session._provisioned = True
    session.project_id = "proj-1"
    session.example_index = {"instA": "ex-1"}
    return session


def test_post_sample_creates_anchor_run_with_instance_id_name(monkeypatch) -> None:
    """The writeback scripts depend on ``run.name == instance_id``."""
    _stub_feedback_config(monkeypatch)
    fake = _FakeClient()
    session = _provisioned_session()

    _post_sample(
        client=fake,
        session=session,
        instance_id="instA",
        sample_input="fix the bug",
        completion="diff goes here",
        sample_metadata={"repo": "owner/proj"},
        scores={"f1": Score(value=0.75)},
    )

    assert len(fake.created_runs) == 1
    run = fake.created_runs[0]
    assert run["name"] == "instA"
    assert run["project_name"] == "ds-solver-202000"
    assert run["reference_example_id"] == "ex-1"
    assert run["inputs"]["instance_id"] == "instA"
    assert run["outputs"] == {"completion": "diff goes here"}

    assert len(fake.created_feedback) == 1
    fb = fake.created_feedback[0]
    assert fb["key"] == "f1"
    assert fb["score"] == 0.75
    assert fb["session_id"] == "proj-1"
    assert fb["run_id"] == run["id"]


def test_post_sample_skips_when_example_missing(monkeypatch) -> None:
    _stub_feedback_config(monkeypatch)
    fake = _FakeClient()
    session = _provisioned_session()

    _post_sample(
        client=fake,
        session=session,
        instance_id="instMISSING",
        sample_input="x",
        completion="y",
        sample_metadata={},
        scores={"f1": Score(value=1.0)},
    )

    assert fake.created_runs == []
    assert fake.created_feedback == []


def test_post_sample_skips_empty_completion(monkeypatch) -> None:
    _stub_feedback_config(monkeypatch)
    fake = _FakeClient()
    session = _provisioned_session()

    _post_sample(
        client=fake,
        session=session,
        instance_id="instA",
        sample_input="x",
        completion="   ",  # blank
        sample_metadata={},
        scores={"f1": Score(value=1.0)},
    )
    assert fake.created_runs == []

    _post_sample(
        client=fake,
        session=session,
        instance_id="instA",
        sample_input="x",
        completion=None,
        sample_metadata={},
        scores={"f1": Score(value=1.0)},
    )
    assert fake.created_runs == []


def test_post_sample_fans_out_precision_and_recall(monkeypatch) -> None:
    """Scorers stash precision/recall in metadata; hook should fan them out."""
    _stub_feedback_config(monkeypatch)
    fake = _FakeClient()
    session = _provisioned_session()

    _post_sample(
        client=fake,
        session=session,
        instance_id="instA",
        sample_input="x",
        completion="y",
        sample_metadata={},
        scores={"review_overlap_f1": Score(value=0.5, metadata={"precision": 0.4, "recall": 0.6})},
    )

    keys = [fb["key"] for fb in fake.created_feedback]
    # The ``_f1`` suffix is stripped to give clean key names like
    # ``review_overlap_precision`` instead of ``review_overlap_f1_precision``.
    assert keys == ["review_overlap_f1", "review_overlap_precision", "review_overlap_recall"]


def test_post_sample_skips_non_numeric_scores(monkeypatch) -> None:
    _stub_feedback_config(monkeypatch)
    fake = _FakeClient()
    session = _provisioned_session()

    _post_sample(
        client=fake,
        session=session,
        instance_id="instA",
        sample_input="x",
        completion="y",
        sample_metadata={},
        scores={"label": Score(value="C")},
    )
    assert len(fake.created_runs) == 1  # anchor still created
    assert fake.created_feedback == []  # but no numeric feedback


# ---------------------------------------------------------------------------
# Hook enabled() — the env-key short-circuit
# ---------------------------------------------------------------------------


def test_hook_disabled_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    hook = _LangSmithExperimentHook()
    assert hook.enabled() is False


def test_hook_disabled_without_project(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "key")
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT_EVAL", raising=False)
    hook = _LangSmithExperimentHook()
    assert hook.enabled() is False


def test_hook_enabled_with_api_key_and_project(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "key")
    monkeypatch.setenv("LANGSMITH_PROJECT_EVAL", "eval-proj")
    hook = _LangSmithExperimentHook()
    assert hook.enabled() is True


# ---------------------------------------------------------------------------
# Hook registration — confirm @hooks decorator landed it in Inspect's registry
# ---------------------------------------------------------------------------


def test_hook_registered_in_inspect_ai_registry() -> None:
    """Importing ``evals.runtime`` must register the hook by name.

    Writeback depends on every Inspect run emitting an anchor; if the hook
    silently fails to register, the symptom is invisible until offline grading
    can't find the run. Catch that here.
    """
    from inspect_ai._util.registry import registry_lookup

    info = registry_lookup("hooks", "openbot_langsmith_experiment")
    assert info is not None


# ---------------------------------------------------------------------------
# on_task_start ordering — session keyed by eval_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_task_end_evicts_session() -> None:
    hook = _LangSmithExperimentHook()
    hook._sessions["e1"] = _make_session()

    end_event = MagicMock()
    end_event.eval_id = "e1"

    await hook.on_task_end(end_event)
    assert "e1" not in hook._sessions


# ---------------------------------------------------------------------------
# on_task_end — aggregate metric push
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_task_end_writes_aggregate_metrics(monkeypatch) -> None:
    """Verify on_task_end calls create_feedback for each aggregate metric."""
    feedbacks: list[dict] = []

    class _FakeClient2:
        def list_datasets(self, **kw):
            return iter([])

        def create_feedback(self, **kw):
            feedbacks.append(kw)

    monkeypatch.setattr("langsmith.Client", lambda: _FakeClient2())
    # Stub ensure_feedback_config so it doesn't fail
    from evals.runtime import langsmith_hook

    monkeypatch.setattr(langsmith_hook, "ensure_feedback_config", lambda *a, **k: None)

    hook = _LangSmithExperimentHook()
    session = _make_session()
    session._provisioned = True
    session.project_id = "proj-agg"
    hook._sessions["e2"] = session

    # Build a fake TaskEnd with results — scores is list[EvalScore]-shaped
    metric_val = MagicMock()
    metric_val.value = 0.75

    eval_score = MagicMock()
    eval_score.scorer = "my_scorer"
    eval_score.metrics = {"mean": metric_val}

    results = MagicMock()
    results.scores = [eval_score]
    results.completed_samples = 10

    log = MagicMock()
    log.results = results

    end_event = MagicMock()
    end_event.eval_id = "e2"
    end_event.log = log

    await hook.on_task_end(end_event)

    assert "e2" not in hook._sessions
    assert len(feedbacks) == 1
    fb = feedbacks[0]
    assert fb["key"] == "my_scorer__mean"
    assert fb["score"] == pytest.approx(0.75)
    assert fb["run_id"] == "proj-agg"


@pytest.mark.asyncio
async def test_on_task_end_noop_when_no_results(monkeypatch) -> None:
    """on_task_end must not call create_feedback when results absent."""
    feedbacks: list[dict] = []

    class _NoClient:
        def create_feedback(self, **kw):
            feedbacks.append(kw)

    monkeypatch.setattr("langsmith.Client", lambda: _NoClient())

    hook = _LangSmithExperimentHook()
    session = _make_session()
    session._provisioned = True
    session.project_id = "proj-noop"
    hook._sessions["e3"] = session

    end_event = MagicMock()
    end_event.eval_id = "e3"
    end_event.log.results = None

    await hook.on_task_end(end_event)

    assert "e3" not in hook._sessions
    assert feedbacks == []

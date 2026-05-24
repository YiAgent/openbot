"""Inspect AI → LangSmith Experiment bridge (hook-based).

Why a hook and not a scorer wrapper:

- Scorer wrappers force every task file to repeat boilerplate
  (``LangSmithExperiment.start(...)`` + ``experiment.wrap(...)``) that has
  nothing to do with what the task evaluates.
- Trace upload is already handled by LangChain auto-tracing
  (``LANGSMITH_TRACING=true`` + ``LANGSMITH_PROJECT_EVAL`` redirect in
  ``evals.runtime.__init__``). The only thing left to handle is the
  *Experiment* table — root Runs anchored to dataset Examples so the
  cross-run dashboard works.
- Inspect AI exposes :class:`inspect_ai.hooks.Hooks` as the canonical
  cross-cutting extension point. Registering once via ``@hooks`` covers
  every task automatically.

What this hook produces in LangSmith:

- One Project (Experiment) per task run, named ``<dataset>-<solver>-<ts>``,
  with ``reference_dataset_id`` set so Examples link to Runs.
- One root Run per scored sample, ``name = instance_id`` so the writeback
  scripts (``evals/scripts/writeback_*_grades.py``) can find it by id and
  attach offline pass@1 feedback after the upstream Docker harness runs.
- Per-sample Feedback for every numeric score the task produced, plus any
  ``precision`` / ``recall`` floats the scorer stashed in score metadata.

Disabled silently (no API calls, no warnings) when ``LANGSMITH_API_KEY`` is
unset. Per-task ``noop`` is also possible: set
``metadata={"langsmith_experiment": False}`` on the Task.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from typing import Any

from inspect_ai.hooks import Hooks, SampleEnd, TaskEnd, TaskStart, hooks
from inspect_ai.scorer import Score

from evals.data._utils import ensure_feedback_config, git_sha, resolve_model_label
from evals.runtime.config import LANGSMITH_EVAL_PROJECT_ENV

logger = logging.getLogger(__name__)


_DEFAULT_FEEDBACK_CONFIG: dict[str, Any] = {"type": "continuous", "min": 0.0, "max": 1.0}


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class _ExperimentSession:
    """Lazy-provisioned LangSmith Experiment project + Example index.

    One instance per task execution. ``project_id`` and ``example_index``
    are populated on first :meth:`provision` call and reused for every
    sample; provisioning failure marks the session inert so subsequent
    samples short-circuit silently.
    """

    def __init__(
        self,
        *,
        dataset_name: str,
        experiment_name: str,
        instance_id_field: str,
        extra_metadata: dict[str, Any],
    ) -> None:
        self.dataset_name = dataset_name
        self.experiment_name = experiment_name
        self.instance_id_field = instance_id_field
        self.extra_metadata = extra_metadata
        self.project_id: str | None = None
        self.example_index: dict[str, str] = {}
        self._provisioned = False
        self._provision_failed = False

    def provision(self) -> None:
        if self._provisioned:
            return
        self._provisioned = True
        try:
            from langsmith import Client

            client = Client()
            ds = next(
                (d for d in client.list_datasets(dataset_name=self.dataset_name)),
                None,
            )
            if ds is None:
                logger.warning(
                    "langsmith hook: dataset %r not found; skipping experiment "
                    "anchor (run the matching build_*_dataset script if you "
                    "need offline-grade writeback)",
                    self.dataset_name,
                )
                self._provision_failed = True
                return
            project = client.create_project(
                project_name=self.experiment_name,
                reference_dataset_id=ds.id,
                description=(
                    f"Inspect AI run of {self.dataset_name} "
                    f"({self.extra_metadata.get('solver_family') or 'unknown'} / "
                    f"{self.extra_metadata.get('model') or 'unknown model'}) "
                    f"at {_now().isoformat()}."
                ),
                metadata=self.extra_metadata,
            )
            self.project_id = str(project.id)
            for ex in client.list_examples(dataset_id=ds.id):
                inputs = ex.inputs or {}
                metadata = ex.metadata or {}
                iid = inputs.get(self.instance_id_field) or metadata.get(self.instance_id_field)
                if iid is not None:
                    self.example_index[str(iid)] = str(ex.id)
            logger.info(
                "langsmith hook: experiment %r ready (project=%s, %d examples)",
                self.experiment_name,
                self.project_id,
                len(self.example_index),
            )
        except Exception as exc:
            logger.warning(
                "langsmith hook: experiment provisioning failed for %r: %s",
                self.experiment_name,
                exc,
            )
            self._provision_failed = True

    @property
    def usable(self) -> bool:
        return bool(self.project_id) and not self._provision_failed


def _build_session(
    *,
    dataset_name: str | None,
    spec_metadata: dict[str, Any],
) -> _ExperimentSession | None:
    if not dataset_name:
        return None
    if spec_metadata.get("langsmith_experiment") is False:
        return None
    solver_family = (
        spec_metadata.get("solver_family") or spec_metadata.get("solver_id") or "unknown"
    )
    model = spec_metadata.get("model") or resolve_model_label()
    instance_id_field = str(spec_metadata.get("instance_id_field") or "instance_id")
    ts = _now().strftime("%Y%m%d-%H%M%S")
    experiment_name = f"{dataset_name}-{solver_family}-{ts}"
    extra: dict[str, Any] = {
        "dataset_name": dataset_name,
        "solver_family": solver_family,
        "model": model.split(":", 1)[-1] if ":" in str(model) else model,
        "git_sha": spec_metadata.get("git_sha") or git_sha(),
        "experiment_name": experiment_name,
    }
    # Forward any task-supplied metadata that isn't an internal control flag.
    for k, v in spec_metadata.items():
        if k in extra or k == "langsmith_experiment":
            continue
        extra[k] = v
    return _ExperimentSession(
        dataset_name=dataset_name,
        experiment_name=experiment_name,
        instance_id_field=instance_id_field,
        extra_metadata=extra,
    )


def _post_sample(
    *,
    client: Any,
    session: _ExperimentSession,
    instance_id: str,
    sample_input: str,
    completion: str | None,
    sample_metadata: dict[str, Any],
    scores: dict[str, Score],
) -> None:
    example_id = session.example_index.get(instance_id)
    if example_id is None:
        logger.debug(
            "langsmith hook: no Example for instance_id=%r in dataset %r; "
            "skipping experiment anchor",
            instance_id,
            session.dataset_name,
        )
        return
    if completion is None or not completion.strip():
        logger.debug(
            "langsmith hook: empty completion for instance_id=%r; skipping",
            instance_id,
        )
        return

    run_id = uuid.uuid4()
    now = _now()
    try:
        client.create_run(
            id=run_id,
            name=instance_id,
            project_name=session.experiment_name,
            run_type="llm",
            inputs={"instance_id": instance_id, "issue": sample_input},
            outputs={"completion": completion},
            start_time=now,
            end_time=now,
            reference_example_id=example_id,
            extra={
                "metadata": {
                    **session.extra_metadata,
                    "instance_id": instance_id,
                    "sample_metadata": sample_metadata,
                }
            },
        )
    except Exception as exc:
        logger.warning(
            "langsmith hook: create_run failed for instance_id=%r: %s",
            instance_id,
            exc,
        )
        return

    for scorer_name, score in scores.items():
        score_value = _coerce_float(getattr(score, "value", None))
        if score_value is None:
            continue
        try:
            ensure_feedback_config(client, scorer_name, _DEFAULT_FEEDBACK_CONFIG)
            client.create_feedback(
                run_id=run_id,
                trace_id=run_id,
                session_id=session.project_id,
                key=scorer_name,
                score=score_value,
                value=score.value,
                comment=getattr(score, "explanation", None),
                source_info={"source": "inspect_scorer", "scorer_name": scorer_name},
                extra={
                    "metadata": {
                        "metric_name": scorer_name,
                        "score_metadata": dict(getattr(score, "metadata", None) or {}),
                    }
                },
                start_time=now,
            )
        except Exception as exc:
            logger.warning(
                "langsmith hook: create_feedback failed for %s/%s: %s",
                instance_id,
                scorer_name,
                exc,
            )
            continue

        # Fan-out precision / recall when the scorer stashed them in metadata.
        meta = getattr(score, "metadata", None) or {}
        for sub in ("precision", "recall"):
            sub_value = _coerce_float(meta.get(sub))
            if sub_value is None:
                continue
            sub_key = (
                f"{scorer_name}_{sub}"
                if not scorer_name.endswith("_f1")
                else (f"{scorer_name[:-2]}{sub}")
            )
            try:
                client.create_feedback(
                    run_id=run_id,
                    trace_id=run_id,
                    session_id=session.project_id,
                    key=sub_key,
                    score=sub_value,
                    value=sub_value,
                    source_info={"source": "inspect_scorer", "scorer_name": scorer_name},
                    extra={"metadata": {"metric_name": sub_key}},
                    start_time=now,
                )
            except Exception as exc:
                logger.warning(
                    "langsmith hook: create_feedback fan-out failed for %s/%s: %s",
                    instance_id,
                    sub_key,
                    exc,
                )


@hooks(
    name="openbot_langsmith_experiment",
    description=(
        "Anchor each Inspect sample to a LangSmith Experiment Run keyed by "
        "instance_id so offline harness writeback can attach pass@1 feedback."
    ),
)
class _LangSmithExperimentHook(Hooks):
    """Hook implementation. One ``_ExperimentSession`` per ``eval_id``."""

    def __init__(self) -> None:
        self._sessions: dict[str, _ExperimentSession] = {}

    def enabled(self) -> bool:
        return bool(os.environ.get("LANGSMITH_API_KEY")) and bool(
            os.environ.get(LANGSMITH_EVAL_PROJECT_ENV) or os.environ.get("LANGSMITH_PROJECT")
        )

    async def on_task_start(self, data: TaskStart) -> None:
        spec = data.spec
        spec_metadata = dict(spec.metadata or {})
        dataset_name = spec_metadata.get("dataset_version") or getattr(spec.dataset, "name", None)
        session = _build_session(
            dataset_name=dataset_name,
            spec_metadata=spec_metadata,
        )
        if session is None:
            return
        self._sessions[data.eval_id] = session

    async def on_sample_end(self, data: SampleEnd) -> None:
        session = self._sessions.get(data.eval_id)
        if session is None:
            return
        if not session._provisioned:
            session.provision()
        if not session.usable:
            return
        from langsmith import Client

        client = Client()
        sample = data.sample
        scores = sample.scores or {}
        completion = sample.output.completion if sample.output is not None else None
        _post_sample(
            client=client,
            session=session,
            instance_id=str(sample.id),
            sample_input=sample.input if isinstance(sample.input, str) else "",
            completion=completion,
            sample_metadata=dict(sample.metadata or {}),
            scores=scores,
        )

    async def on_task_end(self, data: TaskEnd) -> None:
        session = self._sessions.pop(data.eval_id, None)
        if not (session and session.usable):
            return
        if not data.log.results:
            return
        try:
            from langsmith import Client

            client = Client()
            for eval_score in data.log.results.scores or []:
                scorer_name = eval_score.scorer
                for metric_name, metric_val in (eval_score.metrics or {}).items():
                    if metric_val.value is None:
                        continue
                    agg_key = f"{scorer_name}__{metric_name}"
                    ensure_feedback_config(client, agg_key, _DEFAULT_FEEDBACK_CONFIG)
                    client.create_feedback(
                        run_id=session.project_id,
                        key=agg_key,
                        score=float(metric_val.value),
                        comment=(
                            f"aggregate {metric_name} over "
                            f"{data.log.results.completed_samples} samples"
                        ),
                        source_info={"source": "inspect_task_end_hook"},
                    )
        except Exception as exc:
            logger.warning("langsmith hook: on_task_end aggregate failed: %s", exc)

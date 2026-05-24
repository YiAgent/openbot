"""LangSmith trace routing and Experiment projection for Inspect evals.

``configure_tracing_for_dataset`` sets LangChain/LangSmith env vars.
``LangSmithExperiment`` projects Inspect sample scores into LangSmith
Experiment rows with per-sample scores tied back to dataset examples.
``ensure_feedback_config`` reconciles feedback key configs idempotently.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from inspect_ai.scorer import Metric, Score, Scorer, Target, mean, std
from inspect_ai.scorer import scorer as scorer_decorator
from inspect_ai.solver import TaskState

from evals.runtime import config

logger = logging.getLogger(__name__)

_RECONCILED_FEEDBACK_KEYS: set[str] = set()


def ensure_feedback_config(client: Any, key: str, config: dict[str, Any]) -> None:
    """Make LangSmith's stored config for ``key`` match ``config`` (idempotent)."""
    if key in _RECONCILED_FEEDBACK_KEYS:
        return
    try:
        try:
            client.create_feedback_config(feedback_key=key, feedback_config=config)
        except Exception:
            client.update_feedback_config(feedback_key=key, feedback_config=config)
        _RECONCILED_FEEDBACK_KEYS.add(key)
    except Exception as exc:
        logger.warning("langsmith: failed to reconcile feedback config for %r: %s", key, exc)


def reset_feedback_cache_for_tests() -> None:
    """Drop the per-process feedback reconciliation cache (test fixture)."""
    _RECONCILED_FEEDBACK_KEYS.clear()


def configure_tracing_for_dataset(dataset_version: str) -> dict[str, str | None]:
    """Set LangChain / LangSmith env vars so LLM calls auto-trace."""
    del dataset_version  # reserved for future per-dataset routing

    if not os.environ.get("LANGSMITH_API_KEY"):
        return {"project": None, "tracing": None, "source": "skipped:no-api-key"}

    project = os.environ.get(config.LANGSMITH_EVAL_PROJECT_ENV, "")
    if project:
        os.environ["LANGSMITH_PROJECT"] = project

    if "LANGSMITH_TRACING" not in os.environ and "LANGCHAIN_TRACING_V2" not in os.environ:
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGCHAIN_TRACING_V2"] = "true"

    tracing = os.environ.get("LANGSMITH_TRACING") or os.environ.get("LANGCHAIN_TRACING_V2")
    source = "eval-project" if project else "eval-project:unset"
    return {"project": project or None, "tracing": tracing, "source": source}


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _usage_payload(sample_metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Map provider usage metadata onto LangSmith's first-class run fields."""
    raw = (sample_metadata or {}).get("provider_usage")
    if not isinstance(raw, dict):
        return {}

    prompt_tokens = _coerce_int(raw.get("input_tokens", raw.get("prompt_tokens")))
    completion_tokens = _coerce_int(raw.get("output_tokens", raw.get("completion_tokens")))
    total_tokens = _coerce_int(raw.get("total_tokens"))
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens

    payload: dict[str, Any] = {}
    if prompt_tokens is not None:
        payload["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        payload["completion_tokens"] = completion_tokens
    if total_tokens is not None:
        payload["total_tokens"] = total_tokens

    if isinstance(raw.get("input_token_details"), dict):
        payload["prompt_token_details"] = dict(raw["input_token_details"])
    if isinstance(raw.get("output_token_details"), dict):
        payload["completion_token_details"] = dict(raw["output_token_details"])

    total_cost = _coerce_float(raw.get("total_cost"))
    prompt_cost = _coerce_float(raw.get("input_cost", raw.get("prompt_cost")))
    completion_cost = _coerce_float(raw.get("output_cost", raw.get("completion_cost")))
    if total_cost is not None:
        payload["total_cost"] = total_cost
    if prompt_cost is not None:
        payload["prompt_cost"] = prompt_cost
    if completion_cost is not None:
        payload["completion_cost"] = completion_cost

    if isinstance(raw.get("input_cost_details"), dict):
        payload["prompt_cost_details"] = dict(raw["input_cost_details"])
    if isinstance(raw.get("output_cost_details"), dict):
        payload["completion_cost_details"] = dict(raw["output_cost_details"])
    return payload


def _feedback_metric_name(base_key: str, metric_name: str) -> str:
    if metric_name == "f1":
        return base_key
    if base_key.endswith("_f1"):
        return f"{base_key[:-2]}{metric_name}"
    return f"{base_key}_{metric_name}"


def _coerce_completion_output(candidate_completion: str | None) -> Any:
    """Prefer structured JSON over a stringified blob for LangSmith rendering."""
    if candidate_completion is None:
        return None
    try:
        parsed = json.loads(candidate_completion)
    except (TypeError, ValueError, json.JSONDecodeError):
        return candidate_completion
    return parsed if isinstance(parsed, dict | list) else candidate_completion


def _extract_model_patch_output(
    score_metadata: dict[str, Any] | None,
    sample_metadata: dict[str, Any] | None,
    candidate_completion: str | None,
) -> str | None:
    """Prefer an explicit patch field, fall back to the prediction surface."""
    if isinstance(score_metadata, dict):
        patch = score_metadata.get("model_patch")
        if isinstance(patch, str):
            return patch

    prediction = (sample_metadata or {}).get("prediction")
    if isinstance(prediction, dict):
        patch = prediction.get("model_patch")
        if isinstance(patch, str):
            return patch

    parsed_completion = _coerce_completion_output(candidate_completion)
    if isinstance(parsed_completion, dict):
        patch = parsed_completion.get("model_patch")
        if isinstance(patch, str):
            return patch
    return None


@dataclass
class LangSmithExperiment:
    """Per-task LangSmith Experiment session + example index.

    Construct once at ``@task`` build time via :meth:`start`; the resulting
    object holds only metadata (name / dataset / solver_family). The actual
    LangSmith ``project`` is **lazily created on the first :meth:`_post_run`
    call**, so dry-imports and aborted runs no longer leak empty project
    shells onto the LangSmith account.

    All fields except the lazy-init ones (``project_id`` / ``dataset_id`` /
    ``instance_to_example`` / ``_initialized``) are immutable after
    :meth:`start` returns.
    """

    dataset_name: str
    experiment_name: str
    solver_family: str
    model: str | None
    instance_id_field: str
    enabled: bool  # False when LANGSMITH_API_KEY is unset (no-op stub)
    extra_metadata: dict[str, Any] = field(default_factory=dict)

    # Lazy-init state — populated on first _post_run() once we know the run
    # actually produced a score.
    project_id: str | None = None
    dataset_id: str | None = None
    instance_to_example: dict[str, str] = field(default_factory=dict)
    _initialized: bool = False  # True once we've tried to provision LangSmith
    _provision_failed: bool = False  # True if dataset / project lookup failed

    @classmethod
    def start(
        cls,
        *,
        dataset_name: str,
        solver_family: str,
        model: str | None = None,
        git_sha: str | None = None,
        instance_id_field: str = "instance_id",
    ) -> LangSmithExperiment:
        """Build a deferred experiment handle for this task invocation.

        Cheap — just allocates a timestamped experiment name and metadata
        dict. No LangSmith API calls happen here; project creation +
        example indexing are deferred to the first :meth:`_post_run` so
        aborted runs / dry-import passes never produce empty projects.

        Returns a no-op stub (``enabled=False``) when ``LANGSMITH_API_KEY``
        is missing — keeps unit tests / offline dev paths working without
        a hard dep on the network.
        """
        ts = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
        experiment_name = f"{dataset_name}-{solver_family}-{ts}"
        # ``model`` is stored on both the experiment-level metadata and the
        # per-sample run extras. Strip the LangChain ``provider:`` prefix at
        # the LangSmith boundary so dashboard readers see the actual model
        # served, not our internal routing label.
        # Strip provider prefix (e.g. "anthropic:claude-3") for human-readable metadata.
        display_model = model.split(":", 1)[-1] if model and ":" in model else model
        extra_metadata = {
            "dataset_name": dataset_name,
            "solver_family": solver_family,
            "experiment_name": experiment_name,
            "model": display_model,
            "git_sha": git_sha,
        }

        enabled = bool(os.environ.get("LANGSMITH_API_KEY"))
        if not enabled:
            logger.info(
                "LANGSMITH_API_KEY not set — LangSmith Experiment recording is "
                "skipped. Inspect logs (.inspect/logs/...) still capture per-sample "
                "scores for offline review."
            )

        return cls(
            dataset_name=dataset_name,
            experiment_name=experiment_name,
            solver_family=solver_family,
            model=display_model,
            instance_id_field=instance_id_field,
            enabled=enabled,
            extra_metadata=extra_metadata,
        )

    def _provision(self) -> None:
        """Create the LangSmith project + index dataset Examples.

        Called from :meth:`_post_run` exactly once. Sets ``_initialized``
        regardless of outcome so we don't retry on every sample. On
        provisioning failure (missing dataset / network error), sets
        ``_provision_failed`` so subsequent ``_post_run`` calls become
        no-ops with a single warning log.
        """
        self._initialized = True
        if not self.enabled:
            self._provision_failed = True
            return

        # Lazy import — keeps the module cheap for unit tests that never
        # actually post a run.
        from langsmith import Client

        try:
            client = Client()
            ds = next(
                (d for d in client.list_datasets(dataset_name=self.dataset_name)),
                None,
            )
            if ds is None:
                logger.warning(
                    "LangSmith dataset %r not found. Run the matching "
                    "build_*_dataset script. Experiment recording disabled.",
                    self.dataset_name,
                )
                self._provision_failed = True
                return

            project = client.create_project(
                project_name=self.experiment_name,
                reference_dataset_id=ds.id,
                description=(
                    f"Inspect AI run of {self.dataset_name} via "
                    f"{self.solver_family} ({self.model or 'unknown model'}) "
                    f"at {_utc_now_iso()}."
                ),
                metadata=self.extra_metadata,
            )

            instance_to_example: dict[str, str] = {}
            for ex in client.list_examples(dataset_id=ds.id):
                inputs = ex.inputs or {}
                metadata = ex.metadata or {}
                iid = inputs.get(self.instance_id_field) or metadata.get(self.instance_id_field)
                if iid is not None:
                    instance_to_example[str(iid)] = str(ex.id)

            self.project_id = str(project.id)
            self.dataset_id = str(ds.id)
            self.instance_to_example = instance_to_example
            logger.info(
                "LangSmith experiment %r ready (project=%s, dataset=%s, %d examples indexed)",
                self.experiment_name,
                project.id,
                ds.id,
                len(instance_to_example),
            )
        except Exception as exc:
            logger.warning(
                "LangSmith experiment provisioning failed for %r: %s",
                self.experiment_name,
                exc,
            )
            self._provision_failed = True

    def metadata(self) -> dict[str, Any]:
        """Metadata dict to drop into ``Task.metadata`` for trace correlation.

        ``langsmith_project_id`` / ``langsmith_dataset_id`` are ``None``
        until the first sample posts (lazy provisioning); callers that
        want the resolved ids should pull them off the experiment object
        after the run, not from this dict.
        """
        return {
            "langsmith_experiment_name": self.experiment_name,
            "langsmith_project_id": self.project_id,
            "langsmith_dataset_id": self.dataset_id,
        }

    def _post_run(
        self,
        *,
        instance_id: str,
        score: Score,
        sample_input: str,
        candidate_completion: str | None,
        sample_metadata: dict[str, Any] | None,
        scorer_name: str,
        feedback_key: str | None,
        feedback_config: dict[str, Any] | None = None,
    ) -> None:
        # Skip errored / no-output samples. ``--score-on-error`` (when set)
        # forces a zero score even for samples whose solver never produced
        # output, and pushing that to LangSmith creates pure noise: empty
        # Feedback rows that drag the dashboard mean toward zero and bury
        # the real signal under failed retries. The Makefile no longer
        # passes ``--score-on-error`` in normal runs, but this guard keeps
        # the upload clean even if a campaign re-enables it for debugging.
        # Signal we trust: a real completion string. Solvers always set
        # ``state.output.completion`` to their stable JSON envelope on
        # success; on error the field is ``None`` or empty.
        if candidate_completion is None or not candidate_completion.strip():
            logger.debug(
                "skipping LangSmith experiment upload for instance_id=%r — "
                "sample produced no completion (errored or truncated)",
                instance_id,
            )
            return

        # Lazy: first real sample triggers provisioning. Subsequent samples
        # short-circuit when provisioning previously failed (we already
        # logged the warning once, no point retrying per sample).
        if not self._initialized:
            self._provision()
        if self._provision_failed or self.project_id is None:
            return
        example_id = self.instance_to_example.get(instance_id)
        if example_id is None:
            logger.warning(
                "no LangSmith example for instance_id=%r; skipping experiment "
                "run upload (re-run the mirror script if the dataset shifted)",
                instance_id,
            )
            return
        from langsmith import Client

        client = Client()
        run_id = uuid.uuid4()
        sample_metadata = dict(sample_metadata or {})
        usage_payload = _usage_payload(sample_metadata)
        # Pull score value into a float so feedback / metadata are typed.
        try:
            score_value = float(score.value) if score.value is not None else None
        except (TypeError, ValueError):
            score_value = None
        now = dt.datetime.now(dt.UTC)
        try:
            client.create_run(
                id=run_id,
                name=instance_id,
                project_name=self.experiment_name,
                run_type="llm",
                inputs={"instance_id": instance_id, "issue": sample_input},
                # Outputs are the agent artifacts (patch + raw completion). The
                # scalar score lives in Feedback (feedback_key + precision /
                # recall fans below), so we deliberately don't echo it here —
                # doing so made the Outputs column display the F1 number for
                # review runs instead of the actual findings JSON.
                outputs={
                    "model_patch": _extract_model_patch_output(
                        dict(score.metadata or {}),
                        sample_metadata,
                        candidate_completion,
                    ),
                    "completion": _coerce_completion_output(candidate_completion),
                },
                start_time=now,
                end_time=now,
                reference_example_id=example_id,
                extra={
                    "metadata": {
                        **self.extra_metadata,
                        "instance_id": instance_id,
                        "sample_metadata": sample_metadata,
                        "usage_metadata": sample_metadata.get("provider_usage"),
                        "score_explanation": score.explanation,
                    },
                },
                **usage_payload,
            )
            if score_value is not None and feedback_key:
                source_info = {
                    "source": "inspect_scorer",
                    "scorer_name": scorer_name,
                }
                if feedback_config is not None:
                    ensure_feedback_config(client, feedback_key, feedback_config)
                client.create_feedback(
                    run_id=run_id,
                    trace_id=run_id,
                    session_id=self.project_id,
                    key=feedback_key,
                    score=score_value,
                    value=score.value,
                    comment=score.explanation,
                    source_info=source_info,
                    extra={
                        "metadata": {
                            "metric_name": feedback_key,
                            "score_metadata": dict(score.metadata or {}),
                            "sample_metadata": sample_metadata,
                        }
                    },
                    start_time=now,
                )
                for metric_name in ("precision", "recall"):
                    metric_value = _coerce_float((score.metadata or {}).get(metric_name))
                    if metric_value is None:
                        continue
                    metric_key = _feedback_metric_name(feedback_key, metric_name)
                    client.create_feedback(
                        run_id=run_id,
                        trace_id=run_id,
                        session_id=self.project_id,
                        key=metric_key,
                        score=metric_value,
                        value=metric_value,
                        comment=score.explanation,
                        source_info=source_info,
                        extra={"metadata": {"metric_name": metric_key}},
                        start_time=now,
                    )
        except Exception as exc:
            logger.warning(
                "LangSmith Experiment Run upload failed for instance_id=%r: %s",
                instance_id,
                exc,
            )

    def wrap(
        self,
        upstream: Scorer,
        *,
        scorer_name: str,
        feedback_key: str | None,
        metrics: list[Metric] | None = None,
        feedback_config: dict[str, Any] | None = None,
    ) -> Scorer:
        """Return a scorer that runs ``upstream`` then posts to the experiment.

        ``metrics`` defaults to ``[mean(), std()]`` to match upstream
        :func:`inspect_evals.swe_bench.scorers.swe_bench_scorer`'s pass@1
        aggregation. Pass an explicit list to wrap a scorer with different
        metric semantics.

        ``scorer_name`` labels the wrapped scorer in Inspect logs and
        LangSmith feedback metadata — **required**, no default. The previous
        ``"swe_bench_scorer"`` default leaked the SWE-bench semantic onto
        unrelated cells (review/chat/QA), making cross-task dashboards lie.

        ``feedback_key`` is the LangSmith Feedback key used for the upstream
        scalar score — **required**, no default. Pass ``None`` explicitly to
        skip Feedback creation (useful when the upstream scorer already
        attaches richer multi-dim Feedback, e.g. ``swe_qa_pro_*``). The
        previous ``"swe_bench_pass_at_1"`` default once silently mislabeled
        the export-success signal as official pass@1 (see commit history);
        forcing every caller to be explicit prevents recurrence and reserves
        ``swe_bench_pass_at_1`` / ``swt_bench_pass_at_1`` for real offline
        grading writeback.

        Failures during LangSmith upload are logged but never propagated —
        the eval keeps going regardless.
        """

        @scorer_decorator(metrics=metrics or [mean(), std()], name=scorer_name)
        def _wrapped() -> Scorer:
            async def _score(state: TaskState, target: Target) -> Score:
                score = await _await_or_call(upstream, state, target)
                instance_id = str(state.sample_id) if state.sample_id is not None else ""
                self._post_run(
                    instance_id=instance_id,
                    score=score,
                    sample_input=state.input_text,
                    candidate_completion=state.output.completion,
                    sample_metadata=dict(state.metadata or {}),
                    scorer_name=scorer_name,
                    feedback_key=feedback_key,
                    feedback_config=feedback_config,
                )
                return score

            return _score

        return _wrapped()


async def _await_or_call(
    fn: Callable[[TaskState, Target], Any],
    state: TaskState,
    target: Target,
) -> Score:
    """Inspect scorer functions are async; this is a tiny safety net."""
    result = fn(state, target)
    if hasattr(result, "__await__"):
        result = await result
    if isinstance(result, Score):
        return result
    raise TypeError(f"upstream scorer returned {type(result).__name__}, expected Score")

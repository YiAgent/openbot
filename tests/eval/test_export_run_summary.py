"""Tests for solver-aware and telemetry-honest eval summaries."""

from __future__ import annotations

from evals.scripts.export_run_summary import (
    _aggregate,
    _aggregate_langsmith_payload,
    _render_markdown,
    load_langsmith_payload,
    sync_eval_payload_to_langsmith,
)


def _eval_payload() -> dict:
    return {
        "eval": {
            "task": "review_martian_baseline",
            "task_display_name": "review_martian_baseline",
            "dataset": {"name": "martian_smoke_v1", "samples": 1, "sample_ids": ["s1"]},
            "model": "none/none",
            "metadata": {"solver_id": "deepagents_baseline", "solver_family": "baseline"},
        },
        "results": {
            "scores": [
                {
                    "name": "review_overlap_inspect_scorer",
                    "metrics": [{"name": "mean", "value": 1.0}, {"name": "stderr", "value": 0.0}],
                }
            ]
        },
        "samples": [
            {
                "id": "s1",
                "scores": {
                    "review_overlap_inspect_scorer": {
                        "value": 1.0,
                        "metadata": {
                            "precision": 1.0,
                            "recall": 1.0,
                            "candidate_count": 1,
                            "golden_count": 1,
                        },
                        "explanation": "ok",
                    }
                },
                "model_usage": {},
            }
        ],
    }


def test_summary_marks_usage_unavailable_instead_of_zero() -> None:
    agg = _aggregate(_eval_payload())
    assert agg["top_cost_samples"] == [("s1", None)]

    rendered = _render_markdown(agg)
    assert "| `s1` | unavailable |" in rendered


def test_summary_includes_solver_identity() -> None:
    rendered = _render_markdown(_aggregate(_eval_payload()))
    assert "**Solver**: `deepagents_baseline` (`baseline`)" in rendered


def test_summary_uses_provider_usage_when_inspect_usage_is_absent() -> None:
    payload = _eval_payload()
    payload["samples"][0]["metadata"] = {"provider_usage": {"total_cost": 0.25}}

    agg = _aggregate(payload)

    assert agg["top_cost_samples"] == [("s1", 0.25)]


def test_summary_can_render_from_langsmith_payload() -> None:
    payload = {
        "run": {
            "name": "review-martian_smoke_v1-abcdef-opus47-smoke",
            "inputs": {"dataset_name": "martian_smoke_v1"},
            "extra": {
                "metadata": {
                    "suite_name": "review_martian",
                    "dataset_version": "martian_smoke_v1",
                    "solver_id": "deepagents_baseline",
                    "solver_family": "baseline",
                    "model_id": "anthropic/claude-opus-4-7",
                }
            },
        },
        "samples": [
            {
                "sample_id": "s1",
                "score_payload": {
                    "f1": 1.0,
                    "precision": 1.0,
                    "recall": 1.0,
                    "candidate_count": 1,
                    "golden_count": 1,
                },
                "cost_usd": 0.01,
                "failure_category": "none",
            }
        ],
    }

    agg = _aggregate_langsmith_payload(payload)
    assert agg["dataset_name"] == "martian_smoke_v1"
    assert agg["solver_id"] == "deepagents_baseline"
    assert agg["top_cost_samples"] == [("s1", 0.01)]


def test_sync_eval_payload_to_langsmith_logs_run_and_samples(monkeypatch) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.created_runs = []
            self.updated_runs = []
            self.feedback = []

        def create_run(self, **kwargs):
            self.created_runs.append(kwargs)

        def update_run(self, run_id, **kwargs):
            self.updated_runs.append((run_id, kwargs))

        def create_feedback(self, run_id, key, **kwargs):
            self.feedback.append((run_id, key, kwargs))

    artifact_refs: list[tuple[str, str]] = []

    def fake_export_artifact(sample_id, kind, content, **_kwargs):
        artifact_refs.append((sample_id, kind))
        return f"{kind}-ref"

    monkeypatch.setattr(
        "evals.scripts.export_run_summary.export_artifact",
        fake_export_artifact,
    )
    monkeypatch.setattr(
        "evals.scripts.export_run_summary.collect_run_metadata",
        lambda *_args, **_kwargs: {
            "suite_name": "review_martian",
            "suite_version": 1,
            "dataset_version": "martian_smoke_v1",
            "dataset_sha256": "0" * 64,
            "git_sha": "abcdef123456",
            "prompt_version": "unknown",
            "workflow_version": "unknown",
            "solver_id": "deepagents_baseline",
            "solver_family": "baseline",
            "model_id": "anthropic/claude-opus-4-7",
            "judge_model_id": "anthropic/claude-opus-4-7",
            "judge_prompt_version": 1,
            "sandbox_backend": "modal",
            "runner_version": "0.3.220",
            "mode": "smoke",
            "started_at": "2026-05-15T00:00:00Z",
            "triggered_by": "local",
        },
    )
    client = FakeClient()

    run_id = sync_eval_payload_to_langsmith(
        _eval_payload(),
        client=client,
        project_name="openbot-eval-internal",
        dataset_sha256="0" * 64,
        mode="smoke",
    )

    assert client.created_runs[0]["name"].endswith("-smoke")
    assert client.updated_runs[0][0] == run_id
    assert client.feedback[0][1] == "sample::s1"
    assert artifact_refs == [("s1", "diff"), ("s1", "raw_output")]


def test_load_langsmith_payload_reads_run_and_sample_feedback() -> None:
    class FakeFeedback:
        def __init__(self) -> None:
            self.key = "sample::s1"
            self.value = {"sample_id": "s1"}

    class FakeClient:
        def read_run(self, run_id):
            return {"id": run_id}

        def list_feedback(self, *, run_ids):
            assert run_ids == ["run-1"]
            return [FakeFeedback()]

    payload = load_langsmith_payload(FakeClient(), "run-1")

    assert payload == {"run": {"id": "run-1"}, "samples": [{"sample_id": "s1"}]}

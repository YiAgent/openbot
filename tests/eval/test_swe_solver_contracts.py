"""Contract tests for coding-eval deepagents solvers.

Post-refactor: each solver spins up a Modal sandbox via
``DockerSandboxBackend.create_for_sample`` and emits a structured prediction
on ``state.metadata['prediction']``. We monkeypatch the sandbox factory so
the tests stay hermetic — no Modal calls, no network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from evals.solvers import swe_fix, swe_qa, swe_test


class _FakeBackend:
    """Stand-in for :class:`DockerSandboxBackend` in unit tests."""

    def __init__(self, *, sandbox_id: str = "sb-test", diff: str = "diff --git a b\n+x") -> None:
        self.id = sandbox_id
        self._diff = diff
        self.closed = False

    async def acapture_diff(self) -> str:
        return self._diff

    async def aclose(self) -> None:
        self.closed = True


def _patch_backend(monkeypatch: pytest.MonkeyPatch, module: Any, backend: _FakeBackend) -> None:
    """Monkeypatch ``DockerSandboxBackend.create_for_sample`` to yield ``backend``."""

    async def _factory(*, repo_spec, **_kwargs):  # type: ignore[no-untyped-def]
        # Surface repo_spec on the backend for assertions.
        backend.repo_spec = repo_spec  # type: ignore[attr-defined]
        return backend

    monkeypatch.setattr(module.DockerSandboxBackend, "create_for_sample", _factory)


class _FakeAgent:
    def __init__(
        self,
        *,
        content: Any | None = None,
        usage_metadata: dict[str, Any] | None = None,
        messages: list[Any] | None = None,
    ) -> None:
        self.content = content
        self.usage_metadata = usage_metadata
        self.messages = messages
        self.calls: list[dict[str, object]] = []

    async def ainvoke(self, payload, config):  # type: ignore[no-untyped-def]
        self.calls.append({"payload": payload, "config": config})
        if self.messages is not None:
            return {"messages": self.messages}
        return {
            "messages": [SimpleNamespace(content=self.content, usage_metadata=self.usage_metadata)]
        }


def _swe_state(*, dataset_version: str) -> SimpleNamespace:
    return SimpleNamespace(
        input_text="issue body",
        sample_id="sample-1",
        metadata={
            "dataset_version": dataset_version,
            "solver_family": "deepagents_baseline",
            "git_sha": "gitsha",
            "langsmith_experiment_name": "exp",
            "repo": "astropy/astropy",
            "base_commit": "deadbeefcafe",
        },
        output=SimpleNamespace(completion=None),
    )


@pytest.mark.asyncio
async def test_swe_fix_solver_emits_swebench_prediction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diff_body = "diff --git a/foo.py b/foo.py\n+pass\n"
    backend = _FakeBackend(diff=diff_body)
    _patch_backend(monkeypatch, swe_fix, backend)

    agent = _FakeAgent(
        messages=[
            SimpleNamespace(
                content=[{"type": "text", "text": "fixed summary"}],
                usage_metadata={"input_tokens": 21, "output_tokens": 4, "total_cost": 0.55},
            ),
            SimpleNamespace(
                content="Model call limits exceeded: thread limit (20/20)",
                usage_metadata=None,
            ),
        ],
    )
    monkeypatch.setattr(swe_fix, "build_baseline_agent", lambda **_: agent)
    monkeypatch.setattr(swe_fix, "build_run_config", lambda **_: {"cfg": True})

    state = _swe_state(dataset_version="fix_swe_bench_verified")
    out = await swe_fix.deepagents_baseline_swe_solver(model="anthropic:test")(state, None)

    completion = json.loads(out.output.completion)
    assert completion["model_patch"] == diff_body
    assert completion["model_name_or_path"] == "anthropic:test"
    pred = out.metadata["prediction"]
    assert pred["instance_id"] == "sample-1"
    assert pred["model_name_or_path"] == "anthropic:test"
    assert pred["model_patch"] == diff_body
    # Aggregator sums across messages and reconstructs total_tokens when
    # the message omitted it (input + output). Cost is also summed.
    assert out.metadata["provider_usage"] == {
        "input_tokens": 21,
        "output_tokens": 4,
        "total_tokens": 25,
        "total_cost": 0.55,
    }
    assert "fixed summary" in out.metadata["agent_raw_output"]
    assert "Model call limits exceeded" in out.metadata["agent_raw_output"]
    assert backend.closed is True
    # Backend was constructed with the right RepoSpec.
    assert backend.repo_spec.repo == "astropy/astropy"  # type: ignore[attr-defined]
    assert backend.repo_spec.base_commit == "deadbeefcafe"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_swe_fix_solver_emits_empty_prediction_when_metadata_missing() -> None:
    state = SimpleNamespace(
        input_text="issue",
        sample_id="nope",
        metadata={},
        output=SimpleNamespace(completion=None),
    )
    out = await swe_fix.deepagents_baseline_swe_solver(model="anthropic:test")(state, None)
    pred = out.metadata["prediction"]
    assert pred["instance_id"] == "nope"
    assert pred["model_patch"] == ""
    assert "ERROR" in out.output.completion


@pytest.mark.asyncio
async def test_swe_fix_solver_uses_shared_deepagents_model_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    _patch_backend(monkeypatch, swe_fix, backend)

    agent = _FakeAgent(content="fixed summary", usage_metadata=None)
    monkeypatch.setattr(swe_fix, "build_baseline_agent", lambda **_: agent)
    monkeypatch.setattr(swe_fix, "build_run_config", lambda **_: {"cfg": True})
    monkeypatch.delenv("OPENBOT_FIX_MODEL_ID", raising=False)
    monkeypatch.setenv("OPENBOT_DEEPAGENTS_MODEL", "mimo-v2.5")

    state = _swe_state(dataset_version="fix_swe_bench_verified")
    out = await swe_fix.deepagents_baseline_swe_solver()(state, None)

    assert out.metadata["prediction"]["model_name_or_path"] == "anthropic:mimo-v2.5"


@pytest.mark.asyncio
async def test_swt_solver_emits_swtbench_prediction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend(diff="diff --git a/tests/x.py b/tests/x.py\n+def test_x(): pass\n")
    _patch_backend(monkeypatch, swe_test, backend)
    agent = _FakeAgent(
        messages=[
            SimpleNamespace(
                content="test summary",
                usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_cost": 0.2},
            ),
            SimpleNamespace(
                content="Tool budget exhausted, finish with what you have.",
                usage_metadata=None,
            ),
        ],
    )
    monkeypatch.setattr(swe_test, "build_baseline_agent", lambda **_: agent)
    monkeypatch.setattr(swe_test, "build_run_config", lambda **_: {"cfg": True})

    state = _swe_state(dataset_version="test_swt_bench_verified")
    out = await swe_test.deepagents_baseline_swt_solver(model="anthropic:test")(state, None)

    completion = json.loads(out.output.completion)
    assert "test_x" in completion["model_patch"]
    assert completion["model_name_or_path"] == "anthropic:test"
    pred = out.metadata["prediction"]
    assert pred["instance_id"] == "sample-1"
    assert pred["model_name_or_path"] == "anthropic:test"
    assert "test_x" in pred["model_patch"]
    assert out.metadata["provider_usage"] == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "total_cost": 0.2,
    }
    assert "test summary" in out.metadata["agent_raw_output"]
    assert "Tool budget exhausted" in out.metadata["agent_raw_output"]
    assert backend.closed is True


@pytest.mark.asyncio
async def test_swt_solver_uses_shared_deepagents_model_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    _patch_backend(monkeypatch, swe_test, backend)
    agent = _FakeAgent(content="test summary", usage_metadata=None)
    monkeypatch.setattr(swe_test, "build_baseline_agent", lambda **_: agent)
    monkeypatch.setattr(swe_test, "build_run_config", lambda **_: {"cfg": True})
    monkeypatch.delenv("OPENBOT_TEST_MODEL_ID", raising=False)
    monkeypatch.setenv("OPENBOT_DEEPAGENTS_MODEL", "mimo-v2.5")

    state = _swe_state(dataset_version="test_swt_bench_verified")
    out = await swe_test.deepagents_baseline_swt_solver()(state, None)

    assert out.metadata["prediction"]["model_name_or_path"] == "anthropic:mimo-v2.5"


@pytest.mark.asyncio
async def test_swe_qa_agent_solver_consumes_structured_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: agent emits a parsed SweQaProAnswer via structured output."""
    from evals.common.predictions import SweQaProAnswer, SweQaProCitation

    structured = SweQaProAnswer(
        answer="Routing is handled by flask/routing.py.",
        citations=[
            SweQaProCitation(relative_path="flask/routing.py", line_start=1, line_end=10),
        ],
    )

    class _Agent:
        async def ainvoke(self, payload, config):  # type: ignore[no-untyped-def]
            return {
                "structured_response": structured,
                "messages": [SimpleNamespace(content="", usage_metadata=None)],
            }

    backend = _FakeBackend()
    _patch_backend(monkeypatch, swe_qa, backend)
    monkeypatch.setattr(swe_qa, "build_baseline_agent", lambda **_: _Agent())
    monkeypatch.setattr(swe_qa, "build_run_config", lambda **_: {"cfg": True})

    state = SimpleNamespace(
        input_text="What handles routing?",
        sample_id="qa-1",
        metadata={
            "dataset_version": "chat_swe_qa_pro_v1",
            "repo": "pallets/flask",
            "commit_id": "deadbeef",
            "repo_path": "/workspace",
        },
        output=SimpleNamespace(completion=None),
    )
    out = await swe_qa.deepagents_agent_swe_qa_solver(model="anthropic:test")(state, None)

    assert out.output.completion == structured.answer
    pred = out.metadata["prediction"]
    assert pred["answer"] == structured.answer
    assert pred["citations"][0]["relative_path"] == "flask/routing.py"
    assert backend.closed is True


@pytest.mark.asyncio
async def test_swe_qa_agent_solver_falls_back_to_message_text_when_unstructured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protocol violation fallback: no structured_response → use last AI text."""

    class _Agent:
        async def ainvoke(self, payload, config):  # type: ignore[no-untyped-def]
            return {
                "messages": [
                    SimpleNamespace(content="Routing is in flask/routing.py", usage_metadata=None)
                ]
            }

    backend = _FakeBackend()
    _patch_backend(monkeypatch, swe_qa, backend)
    monkeypatch.setattr(swe_qa, "build_baseline_agent", lambda **_: _Agent())
    monkeypatch.setattr(swe_qa, "build_run_config", lambda **_: {"cfg": True})

    state = SimpleNamespace(
        input_text="Q?",
        sample_id="qa-2",
        metadata={
            "dataset_version": "chat_swe_qa_pro_v1",
            "repo": "x/y",
            "commit_id": "abc",
            "repo_path": "/workspace",
        },
        output=SimpleNamespace(completion=None),
    )
    out = await swe_qa.deepagents_agent_swe_qa_solver()(state, None)
    assert out.output.completion == "Routing is in flask/routing.py"
    # Fallback predicate emits an empty citations list.
    assert out.metadata["prediction"]["citations"] == []

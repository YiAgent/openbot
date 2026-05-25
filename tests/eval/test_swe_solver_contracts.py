"""Contract tests for OpenBot thin-solver Inspect AI adapters.

Each solver delegates to the openbot.evaluation facade rather than
driving deepagents directly. The tests monkeypatch the facade entry
points (run_fix_sample, run_chat_sample) and sandbox factory so the
contracts stay hermetic — no network, no real sandbox, no LLM.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from evals.solvers.chat import openbot_chat_solver
from evals.solvers.fix import openbot_fix_solver
from evals.solvers.test_generation import openbot_test_generation_solver

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _state(
    *,
    input_text: str = "issue body",
    sample_id: str = "sample-1",
    repo: str = "astropy/astropy",
    base_commit: str = "deadbeefcafe",
    commit_id: str = "",
    extra_meta: dict[str, Any] | None = None,
) -> SimpleNamespace:
    md: dict[str, Any] = {"repo": repo, "base_commit": base_commit}
    if commit_id:
        md["commit_id"] = commit_id
    if extra_meta:
        md.update(extra_meta)
    return SimpleNamespace(
        input_text=input_text,
        sample_id=sample_id,
        metadata=md,
        output=SimpleNamespace(completion=None),
    )


class _FakeSandbox:
    """Minimal SandboxPort stand-in."""

    id = "sb-test"
    workspace = "/workspace"
    entered = False
    exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_):
        self.exited = True


class _FakeFixOutcome:
    """Minimal FixOutcome stand-in returned by run_fix_sample."""

    class _Attempt:
        diff = "diff --git a/foo.py b/foo.py\n+pass\n"

    attempt = _Attempt()


# ---------------------------------------------------------------------------
# openbot_fix_solver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_solver_emits_swebench_prediction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: sandbox configured, run_fix_sample returns a diff."""
    sandbox = _FakeSandbox()

    @asynccontextmanager
    async def _factory():
        async with sandbox as sb:
            yield sb

    monkeypatch.setattr(
        "evals.solvers.fix.build_sandbox_factory",
        lambda *_, **__: _factory,
    )
    monkeypatch.setattr(
        "evals.solvers.fix.run_fix_sample",
        lambda **_: _async_return(_FakeFixOutcome()),
    )

    state = _state()
    out = await openbot_fix_solver()(state, None)

    completion = json.loads(out.output.completion)
    assert completion["instance_id"] == "sample-1"
    assert completion["model_patch"] == _FakeFixOutcome._Attempt.diff
    pred = out.metadata["prediction"]
    assert pred["instance_id"] == "sample-1"
    assert pred["model_patch"] == _FakeFixOutcome._Attempt.diff


@pytest.mark.asyncio
async def test_fix_solver_empty_prediction_when_repo_missing() -> None:
    """Missing repo/base_commit → empty prediction, no sandbox call."""
    state = _state(repo="", base_commit="")
    out = await openbot_fix_solver()(state, None)

    pred = out.metadata["prediction"]
    assert pred["instance_id"] == "sample-1"
    assert pred["model_patch"] == ""
    assert "ERROR" in out.output.completion


@pytest.mark.asyncio
async def test_fix_solver_empty_prediction_when_no_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DAYTONA_API_KEY → build_sandbox_factory returns None → empty prediction."""
    monkeypatch.setattr(
        "evals.solvers.fix.build_sandbox_factory",
        lambda *_, **__: None,
    )

    state = _state()
    out = await openbot_fix_solver()(state, None)

    pred = out.metadata["prediction"]
    assert pred["model_patch"] == ""
    assert "no sandbox" in out.output.completion.lower()


@pytest.mark.asyncio
async def test_fix_solver_emits_empty_diff_when_attempt_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_fix_sample outcome with attempt=None → patch is empty string."""

    class _NoAttempt:
        attempt = None

    @asynccontextmanager
    async def _factory():
        yield _FakeSandbox()

    monkeypatch.setattr("evals.solvers.fix.build_sandbox_factory", lambda *_, **__: _factory)
    monkeypatch.setattr(
        "evals.solvers.fix.run_fix_sample",
        lambda **_: _async_return(_NoAttempt()),
    )

    state = _state()
    out = await openbot_fix_solver()(state, None)

    assert out.metadata["prediction"]["model_patch"] == ""


# ---------------------------------------------------------------------------
# openbot_chat_solver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_solver_emits_answer_as_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: run_chat_sample returns a string answer."""
    monkeypatch.setattr(
        "evals.solvers.chat.run_chat_sample",
        lambda **_: _async_return("Routing is in flask/routing.py."),
    )

    state = _state(
        input_text="What handles routing?",
        sample_id="qa-1",
        repo="pallets/flask",
        base_commit="",  # chat solver doesn't use base_commit
    )
    out = await openbot_chat_solver()(state, None)

    assert out.output.completion == "Routing is in flask/routing.py."


@pytest.mark.asyncio
async def test_chat_solver_passes_repo_and_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_chat_sample receives repo + user_request from task state."""
    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr("evals.solvers.chat.run_chat_sample", _capture)

    state = _state(input_text="Explain the fix.", repo="pallets/flask", sample_id="qa-2")
    await openbot_chat_solver()(state, None)

    assert captured["repo"] == "pallets/flask"
    assert captured["user_request"] == "Explain the fix."
    assert captured["run_id"] == "qa-2"


# ---------------------------------------------------------------------------
# openbot_test_generation_solver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_generation_solver_emits_unsupported_flag() -> None:
    """Always emits unsupported=True — capability not yet in v0.1."""
    state = _state(sample_id="swt-1")
    out = await openbot_test_generation_solver()(state, None)

    assert out.metadata["unsupported"] is True
    assert out.metadata["unsupported_reason"] == "not_implemented"
    pred = out.metadata["prediction"]
    assert pred["instance_id"] == "swt-1"
    assert pred["model_patch"] == ""
    assert "UNSUPPORTED" in out.output.completion


@pytest.mark.asyncio
async def test_test_generation_solver_stores_prediction_json() -> None:
    """prediction_json key is always present for export."""
    state = _state(sample_id="swt-2")
    out = await openbot_test_generation_solver()(state, None)

    payload = json.loads(out.metadata["prediction_json"])
    assert payload["instance_id"] == "swt-2"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _async_return(value: Any) -> Any:
    return value

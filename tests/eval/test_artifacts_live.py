"""Live LangSmith integration tests for evals.common.artifacts.export_artifact.

These tests hit the real LangSmith API. They auto-skip when
`LANGSMITH_API_KEY` is absent (CI without secrets, fresh checkouts, etc.),
so the rest of the suite stays clean.

To run: `doppler run --project openbot --config dev -- uv run pytest tests/eval/test_artifacts_live.py -v`
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LANGSMITH_API_KEY"),
    reason="needs live LANGSMITH_API_KEY (set via Doppler or env)",
)


def _internal_project() -> str:
    return os.environ.get("LANGSMITH_PROJECT_INTERNAL", "openbot-eval-internal")


def _public_project() -> str:
    return os.environ.get("LANGSMITH_PROJECT_PUBLIC", "openbot-eval-public")


def _wait_for_run(client, run_id: str, *, timeout_s: float = 10.0):
    """Poll until the run is visible to GET /runs/{id} (LangSmith writes are async)."""
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return client.read_run(run_id)
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    raise AssertionError(f"run {run_id} never became visible: {last_err}")


def test_export_artifact_round_trip_to_internal() -> None:
    """Upload an artifact via export_artifact; verify the run lands in internal project."""
    from langsmith import Client

    from evals.common.artifacts import export_artifact

    sample_id = f"live-smoke-{uuid.uuid4().hex[:8]}"
    payload = b"diff --git a/x b/x\n+changed\n"

    client = Client()
    run_id = export_artifact(
        sample_id,
        "patch",
        payload,
        client=client,
        project_name=_internal_project(),
    )
    assert run_id

    run = _wait_for_run(client, run_id)
    session_id = getattr(run, "session_id", None)
    assert session_id is not None
    public_session_id = client.read_project(project_name=_public_project()).id
    leak_msg = f"artifact leaked into public project {public_session_id} — PRD §13.2 violation"
    assert str(session_id) != str(public_session_id), leak_msg
    assert getattr(run, "name", "") == f"artifact::patch::{sample_id}"


def test_export_artifact_rejects_unknown_kind() -> None:
    """Whitebox: invalid kind raises before any HTTP call."""
    from evals.common.artifacts import export_artifact

    with pytest.raises(ValueError, match="unknown ArtifactKind"):
        export_artifact("any", "screenshot", b"...")  # type: ignore[arg-type]


def test_export_artifact_string_content_is_utf8_encoded() -> None:
    """A `str` payload reaches LangSmith as utf-8 bytes (no TypeError)."""
    from langsmith import Client

    from evals.common.artifacts import export_artifact

    sample_id = f"live-smoke-utf8-{uuid.uuid4().hex[:8]}"
    client = Client()
    run_id = export_artifact(
        sample_id,
        "log",
        "héllo 世界\n",
        client=client,
        project_name=_internal_project(),
    )
    run = _wait_for_run(client, run_id)
    assert getattr(run, "name", "") == f"artifact::log::{sample_id}"

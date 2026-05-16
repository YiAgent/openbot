"""Unit tests for LangSmith artifact export."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from evals.common.artifacts import export_artifact


@dataclass
class _FakeClient:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create_run(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def test_export_artifact_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown ArtifactKind"):
        export_artifact("sample-1", "screenshot", b"...")  # type: ignore[arg-type]


def test_export_artifact_encodes_string_payload() -> None:
    client = _FakeClient()

    export_artifact("sample-1", "log", "héllo", client=client)

    attachment = client.calls[0]["attachments"]["artifact::log"]
    assert attachment == ("text/plain", "héllo".encode())


def test_export_artifact_creates_named_run_and_returns_id() -> None:
    client = _FakeClient()

    run_id = export_artifact(
        "sample-1",
        "patch",
        b"diff --git a/x b/x\n",
        client=client,
        project_name="openbot-eval-internal",
    )

    call = client.calls[0]
    assert run_id == str(call["id"])
    assert call["name"] == "artifact::patch::sample-1"
    assert call["run_type"] == "tool"
    assert call["project_name"] == "openbot-eval-internal"
    assert call["inputs"] == {"sample_id": "sample-1", "kind": "patch"}
    assert call["outputs"] == {"artifact_name": "artifact::patch"}

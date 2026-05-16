"""Artifact export to LangSmith — eval PRD §10.2."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

ArtifactKind = Literal["patch", "log", "trace", "diff", "raw_output"]
_ALLOWED_KINDS: frozenset[str] = frozenset({"patch", "log", "trace", "diff", "raw_output"})
_MIME_BY_KIND: dict[str, str] = {
    "patch": "text/x-diff",
    "diff": "text/x-diff",
    "log": "text/plain",
    "trace": "application/json",
    "raw_output": "application/octet-stream",
}


def export_artifact(
    sample_id: str,
    kind: ArtifactKind,
    content: bytes | str,
    *,
    client: Any | None = None,
    project_name: str | None = None,
) -> str:
    """Upload an artifact for a given sample, return a LangSmith artifact ref.

    Parameters
    ----------
    sample_id
        Dataset-unique sample id (matches `evals.common._metadata_spec.REQUIRED_SAMPLE_FIELDS`).
    kind
        One of `patch | log | trace | diff | raw_output`. Used as the artifact key prefix.
    content
        Raw bytes (preferred) or utf-8 string. Caller owns serialization.
    client
        Optional pre-built LangSmith client (use `evals.common.langsmith.init_client()`).
        When None, the live implementation will build one on demand.

    Returns
    -------
    str
        LangSmith artifact ref / URL — caller appends it to
        `sample["input_artifact_refs"]` or `output_artifact_refs`.

    """
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"unknown ArtifactKind {kind!r}; expected {sorted(_ALLOWED_KINDS)}")

    payload = content.encode("utf-8") if isinstance(content, str) else content
    run_id = uuid4()
    now = datetime.now(UTC)

    if client is None:
        from evals.common.langsmith import init_client

        client = init_client(public=False)

    attachment_name = f"artifact::{kind}"
    client.create_run(
        id=run_id,
        name=f"{attachment_name}::{sample_id}",
        run_type="tool",
        inputs={"sample_id": sample_id, "kind": kind},
        outputs={"artifact_name": attachment_name},
        project_name=project_name,
        attachments={attachment_name: (_MIME_BY_KIND[kind], payload)},
        start_time=now,
        end_time=now,
        extra={"metadata": {"sample_id": sample_id, "artifact_kind": kind}},
    )
    return str(run_id)

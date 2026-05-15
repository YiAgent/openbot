"""Artifact export to LangSmith — eval PRD §10.2.

**E1-T04 SKELETON ONLY.** The real LangSmith artifact upload requires a live
`LANGSMITH_API_KEY` (PRD §10.2 mandates integration test against the real API
on ≥ 1 sample). That credential is not available in this stack, so the
function below is a documented stub that raises `NotImplementedError`.

Downstream tasks (E1-T06 review solver, E1-T08 Martian smoke task) can still
import `export_artifact` as a name — they just must not call it until the
real implementation lands.

See `docs/eval/handoffs/E1-T04.md` for unblock conditions.
"""

from __future__ import annotations

from typing import Any, Literal

ArtifactKind = Literal["patch", "log", "trace", "diff", "raw_output"]


def export_artifact(
    sample_id: str,
    kind: ArtifactKind,
    content: bytes | str,
    *,
    client: Any | None = None,
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

    Notes
    -----
    Stub raises `NotImplementedError`. Replace the body when LANGSMITH_API_KEY
    is available; the signature is stable.
    """
    raise NotImplementedError(
        "E1-T04 artifact upload requires a live LANGSMITH_API_KEY. "
        "See docs/eval/handoffs/E1-T04.md for the unblock checklist."
    )

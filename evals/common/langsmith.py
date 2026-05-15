"""LangSmith client wrapper — eval PRD §10.1 / §10.2 / §13.2.

Three responsibilities:
  - init_client()         → returns a LangSmith Client (read API key from env)
  - log_run_metadata()    → validates every PRD §10.1 field is present, else raises
  - log_sample()          → validates every PRD §10.2 field is present, else raises

The actual LangSmith client lives in `langsmith.Client` (already an indirect
dep via inspect-ai). For tests, inject a mock client — both `log_*` functions
take it as their first arg.

Project routing (PRD §13.2): if `dataset_version` resolves to a public dataset,
route to `LANGSMITH_PROJECT_PUBLIC`; else `LANGSMITH_PROJECT_INTERNAL`. The
routing predicate is wired in E1-T10 — until then `init_client(public=False)`
just reads `LANGSMITH_PROJECT_INTERNAL`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Protocol

from evals.common._metadata_spec import (
    REQUIRED_RUN_FIELD_NAMES,
    REQUIRED_SAMPLE_FIELD_NAMES,
)


class _LangSmithLike(Protocol):
    """Structural type — anything with the two methods we touch."""

    def update_run(self, run_id: str, **kwargs: Any) -> Any: ...

    def create_feedback(self, run_id: str, key: str, **kwargs: Any) -> Any: ...


def init_client(*, public: bool = False) -> _LangSmithLike:
    """Build a LangSmith Client reading credentials from env.

    Required env vars:
      - LANGSMITH_API_KEY
      - LANGSMITH_PROJECT_INTERNAL (when public=False)
      - LANGSMITH_PROJECT_PUBLIC   (when public=True)

    Raises ValueError if any required env var is missing — fail loud, never
    silently route a real run into the wrong project.
    """
    api_key = os.environ.get("LANGSMITH_API_KEY")
    if not api_key:
        raise ValueError("LANGSMITH_API_KEY is not set (PRD §13.2 routing requirement)")

    project_env = "LANGSMITH_PROJECT_PUBLIC" if public else "LANGSMITH_PROJECT_INTERNAL"
    project = os.environ.get(project_env)
    if not project:
        raise ValueError(f"{project_env} is not set (PRD §13.2 routing requirement)")

    # Imported lazily — keeps this module importable in environments where
    # `langsmith` isn't installed yet (e.g. the validator script's --help path).
    from langsmith import Client

    return Client(api_key=api_key)


def _missing(required: frozenset[str], provided: Mapping[str, Any]) -> list[str]:
    return sorted(name for name in required if name not in provided)


def log_run_metadata(client: _LangSmithLike, run_id: str, metadata: Mapping[str, Any]) -> None:
    """Attach run-level metadata to a LangSmith run. PRD §10.1 contract.

    Raises ValueError if any required field is missing. Caller is expected to
    populate metadata via `evals.common.metadata.collect_run_metadata()` (E1-T03).
    """
    missing = _missing(REQUIRED_RUN_FIELD_NAMES, metadata)
    if missing:
        raise ValueError(
            f"Run metadata missing required PRD §10.1 fields: {missing}. "
            f"Provided keys: {sorted(metadata.keys())}"
        )
    client.update_run(run_id, extra={"metadata": dict(metadata)})


def log_sample(client: _LangSmithLike, run_id: str, sample: Mapping[str, Any]) -> None:
    """Attach a sample-level record to a LangSmith run. PRD §10.2 contract."""
    missing = _missing(REQUIRED_SAMPLE_FIELD_NAMES, sample)
    if missing:
        raise ValueError(
            f"Sample missing required PRD §10.2 fields: {missing}. "
            f"Provided keys: {sorted(sample.keys())}"
        )
    client.create_feedback(
        run_id,
        key=f"sample::{sample['sample_id']}",
        score=sample.get("score_payload"),
        value=dict(sample),
    )

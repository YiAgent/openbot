"""LangSmith client wrapper — eval PRD §10.1 / §10.2 / §13.2.

Four responsibilities:
  - init_client(*, public)              → returns a LangSmith Client (env-driven)
  - init_client_for_dataset(version)    → routes via manifest.public (PRD §13.2)
  - log_run_metadata()                  → validates PRD §10.1 fields, else raises
  - log_sample()                        → validates PRD §10.2 fields, else raises

The actual LangSmith client lives in `langsmith.Client` (already an indirect
dep via inspect-ai). For tests, inject a mock client — both `log_*` functions
take it as their first arg.

Project routing (PRD §13.2): each dataset manifest carries a `public: bool`
flag (PRD §7.2 schema). `is_public_dataset(version)` reads the manifest and
returns the flag; `init_client_for_dataset(version)` calls `init_client` with
that flag so a single dataset id selects the right LangSmith project.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Mapping
from typing import Any, Protocol

from evals.common._metadata_spec import (
    REQUIRED_RUN_FIELD_NAMES,
    REQUIRED_SAMPLE_FIELD_NAMES,
)

# `evals/datasets/manifests/` lives at repo-root/evals/datasets/manifests/.
_MANIFEST_DIR = pathlib.Path(__file__).resolve().parent.parent / "datasets" / "manifests"


class _LangSmithLike(Protocol):
    """Structural type — anything with the two methods we touch."""

    def update_run(self, run_id: str, **kwargs: Any) -> Any: ...

    def create_feedback(self, run_id: str, key: str, **kwargs: Any) -> Any: ...


def is_public_dataset(dataset_version: str, *, manifest_dir: pathlib.Path | None = None) -> bool:
    """Read `<dataset_version>.yaml` manifest and return its `public` field.

    PRD §7.2 schema mandates a `public: bool` key on every manifest. Missing
    manifest or missing field is treated as **internal** (`False`) — fail safe:
    a misconfigured dataset must never accidentally leak into the public
    LangSmith project (which is the SOC of PRD §13.2).
    """
    root = manifest_dir or _MANIFEST_DIR
    manifest = root / f"{dataset_version}.yaml"
    if not manifest.exists():
        return False
    text = manifest.read_text(encoding="utf-8")
    # Hand-rolled minimal YAML scan to avoid pulling in a yaml dep at import time;
    # the only field we care about is `public: <bool>` at top level.
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("public:"):
            continue
        value = line.split(":", 1)[1].strip().lower()
        return value in {"true", "yes", "1"}
    return False


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


def init_client_for_dataset(dataset_version: str) -> _LangSmithLike:
    """Sugar: pick the right project based on the dataset's manifest.public flag.

    Caller-friendly entry point — task files (E1-T08+) just say::

        client = init_client_for_dataset(metadata["dataset_version"])
    """
    return init_client(public=is_public_dataset(dataset_version))


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
    """Attach a sample-level record to a LangSmith run. PRD §10.2 + §12.4 contract."""
    missing = _missing(REQUIRED_SAMPLE_FIELD_NAMES, sample)
    if missing:
        raise ValueError(
            f"Sample missing required PRD §10.2 fields: {missing}. "
            f"Provided keys: {sorted(sample.keys())}"
        )
    # E2-T16 (Codex P2): enforce PRD §12.4 failure_category enum at the
    # actual write surface, not just in the validator script. This is the
    # call site that turns the previously-dead helper into a real gate.
    from evals.common._metadata_spec import ALLOWED_FAILURE_CATEGORIES

    fc = sample.get("failure_category")
    if fc not in ALLOWED_FAILURE_CATEGORIES:
        raise ValueError(
            f"failure_category {fc!r} is not in the PRD §12.4 enum. "
            f"Allowed: {sorted(ALLOWED_FAILURE_CATEGORIES)}"
        )
    client.create_feedback(
        run_id,
        key=f"sample::{sample['sample_id']}",
        score=(sample.get("score_payload") or {}).get("f1"),
        value=dict(sample),
    )

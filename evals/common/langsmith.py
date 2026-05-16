"""LangSmith trace routing for Inspect AI tasks.

The eval pipeline is:

  - Datasets live in **LangSmith** (published by ``evals/scripts/build_*_dataset.py``).
  - Inspect AI orchestrates runs (pulling Examples via
    ``evals.common.datasets.langsmith_dataset``).
  - Run results land in LangSmith as ``ChatAnthropic`` / LangChain traces,
    routed to the public or internal project per the allowlist below.

The allowlist is a code-level constant (replaces the prior on-disk manifest
scan) — datasets are now part of the project's compile-time surface, not
configuration that can drift.
"""

from __future__ import annotations

import os

# PRD §13.2 routing — public-shareable datasets go to LANGSMITH_PROJECT_PUBLIC;
# everything else (including unknown names) falls through to internal.
_PUBLIC_DATASETS: frozenset[str] = frozenset(
    {
        "martian_2026w20",
    }
)


def is_public_dataset(dataset_version: str) -> bool:
    """Return True when ``dataset_version`` is a publicly-shareable dataset.

    Unknown names route to the internal LangSmith project — fail safe.
    """
    return dataset_version in _PUBLIC_DATASETS


def configure_tracing_for_dataset(dataset_version: str) -> dict[str, str | None]:
    """Set LangChain / LangSmith env vars so this process's LLM calls auto-trace.

    Idempotent. Picks the project from the ``_PUBLIC_DATASETS`` allowlist:

      - in allowlist → ``LANGSMITH_PROJECT_PUBLIC``
      - otherwise    → ``LANGSMITH_PROJECT_INTERNAL``

    Behavior:
      - Skips entirely (``source=skipped:no-api-key``) if ``LANGSMITH_API_KEY``
        is unset — don't crash unit tests / dry runs.
      - **Always** routes by allowlist. A pre-existing ``LANGSMITH_PROJECT`` is
        overwritten; the prior "caller override wins" rule was a footgun
        because Doppler's default value silently absorbed every eval trace.
      - Sets both ``LANGSMITH_TRACING`` (langsmith ≥0.8) and
        ``LANGCHAIN_TRACING_V2`` (legacy langchain-core readers) when neither
        is already set, so ``langchain_anthropic.ChatAnthropic`` calls inside
        deepagents / the judge auto-publish runs.

    Returns the resolved state (for logging / sanity-check).
    """
    if not os.environ.get("LANGSMITH_API_KEY"):
        return {"project": None, "tracing": None, "source": "skipped:no-api-key"}

    public = is_public_dataset(dataset_version)
    env_name = "LANGSMITH_PROJECT_PUBLIC" if public else "LANGSMITH_PROJECT_INTERNAL"
    project = os.environ.get(env_name, "")
    source = f"allowlist-{'public' if public else 'internal'}"
    if project:
        os.environ["LANGSMITH_PROJECT"] = project

    if "LANGSMITH_TRACING" not in os.environ and "LANGCHAIN_TRACING_V2" not in os.environ:
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGCHAIN_TRACING_V2"] = "true"

    tracing = os.environ.get("LANGSMITH_TRACING") or os.environ.get("LANGCHAIN_TRACING_V2")
    return {"project": project or None, "tracing": tracing, "source": source}

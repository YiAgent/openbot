"""Shim: re-exports from evals.data._utils.

NOTE: LangSmithExperiment + configure_tracing_for_dataset are NOT re-exported.
They are eliminated by the evals-data unification refactor.
Any caller must be migrated to evals.data._utils directly.

This file will be deleted in Task 16.
"""

from evals.data._utils import (  # noqa: F401
    ensure_feedback_config,
    git_sha,
    reset_feedback_cache_for_tests,
    resolve_model_label,
)

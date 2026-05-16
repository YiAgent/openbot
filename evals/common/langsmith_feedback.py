"""Shared LangSmith feedback-config reconciliation.

LangSmith stores feedback configs at the *org* level, keyed by feedback
key, with **first-write-wins** semantics: the first ``create_feedback``
call for a new key seeds the stored config, and subsequent calls with a
different ``feedback_config`` payload 400 with ``feedback config
mismatch``.

This module exposes an idempotent, per-process-cached
:func:`ensure_feedback_config` that reconciles the stored config to a
canonical shape before any ``create_feedback`` is issued. Used both by
:mod:`evals.scorers.swe_qa_pro` (multi-dim trace feedback) and
:mod:`evals.common.langsmith_experiments` (single-scalar Experiment Run
feedback) so any new feedback key declared anywhere in this codebase
gets a consistent range/type registered on first contact.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Keys already reconciled this process; cached so we don't hit LangSmith
# every sample / every dim. Bounded by the number of distinct feedback
# keys (≪ 50) so memory is a non-issue.
_RECONCILED_FEEDBACK_KEYS: set[str] = set()


def ensure_feedback_config(client: Any, key: str, config: dict[str, Any]) -> None:
    """Make LangSmith's stored config for ``key`` match ``config``.

    Try ``create_feedback_config`` first (idempotent on identical
    payload). On conflict — stored config exists but differs — PATCH
    via ``update_feedback_config`` instead. Both calls are best-effort:
    any LangSmith error is logged once at warning and swallowed so a
    misconfigured tracing project never blocks an eval.

    Args:
        client: An instantiated ``langsmith.Client``. Caller manages
            lifetime; we don't hold a reference.
        key: Feedback key (e.g. ``"swe_qa_pro_overall_50"``).
        config: Canonical feedback config dict, e.g.
            ``{"type": "continuous", "min": 0.0, "max": 1.0}``.
    """
    if key in _RECONCILED_FEEDBACK_KEYS:
        return
    try:
        try:
            client.create_feedback_config(feedback_key=key, feedback_config=config)
        except Exception:
            # Conflict (different stored config). PATCH to canonical shape.
            client.update_feedback_config(feedback_key=key, feedback_config=config)
        _RECONCILED_FEEDBACK_KEYS.add(key)
    except Exception as exc:
        # Don't cache failure — next sample retries silently. One warning
        # per session is enough surface area for ops to notice.
        logger.warning(
            "langsmith_feedback: failed to reconcile config for key %r: %s",
            key,
            exc,
        )


def reset_cache_for_tests() -> None:
    """Drop the per-process reconciliation cache (test fixture helper)."""
    _RECONCILED_FEEDBACK_KEYS.clear()

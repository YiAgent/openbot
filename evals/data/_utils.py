"""Minimal utilities used by suite build_task() methods and the langsmith hook.

Intentionally small: experiment lifecycle (LangSmithExperiment, configure_tracing,
build_export_experiment) is NOT here — it belongs entirely to langsmith_hook.py.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

_RECONCILED_FEEDBACK_KEYS: set[str] = set()


def git_sha() -> str:
    """Best-effort current git SHA for run metadata."""
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def resolve_model_label() -> str:
    """Best-effort model label for task metadata (not a routing decision)."""
    try:
        from openbot.core.settings import Settings

        return Settings().model or "openbot"
    except Exception:
        return "openbot"


def ensure_feedback_config(client: Any, key: str, config: dict[str, Any]) -> None:
    """Idempotently reconcile a LangSmith feedback key's display config."""
    if key in _RECONCILED_FEEDBACK_KEYS:
        return
    try:
        try:
            client.create_feedback_config(feedback_key=key, feedback_config=config)
        except Exception:
            client.update_feedback_config(feedback_key=key, feedback_config=config)
        _RECONCILED_FEEDBACK_KEYS.add(key)
    except Exception as exc:
        logger.warning("langsmith: failed to reconcile feedback config for %r: %s", key, exc)


def reset_feedback_cache_for_tests() -> None:
    """Drop the per-process feedback reconciliation cache (test fixture only)."""
    _RECONCILED_FEEDBACK_KEYS.clear()

"""Metrics — stable export names and singleton behaviour."""

from __future__ import annotations

from openbot.core import metrics as metrics_mod


def test_workflow_total_is_exported() -> None:
    assert hasattr(metrics_mod, "workflow_total")


def test_queue_depth_is_exported() -> None:
    assert hasattr(metrics_mod, "queue_depth")


def test_llm_cost_usd_total_is_exported() -> None:
    assert hasattr(metrics_mod, "llm_cost_usd_total")


def test_workflow_total_is_singleton() -> None:
    """Module-level objects must be singletons — callers store references at import time."""
    from openbot.core.metrics import workflow_total as a
    from openbot.core.metrics import workflow_total as b

    assert a is b


def test_all_exports_are_accessible() -> None:
    """Every name in __all__ must be importable."""
    for name in metrics_mod.__all__:
        assert hasattr(metrics_mod, name), f"Missing export: {name}"

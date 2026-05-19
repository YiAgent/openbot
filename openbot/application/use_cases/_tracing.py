"""LangSmith traceable decorator — shared import with graceful fallback.

All four use-case entry points import ``traceable`` from here rather than
from ``langsmith`` directly so:

  1. The ``ImportError`` guard lives in one place.
  2. A slim image without ``langsmith`` installed still boots cleanly —
     the fallback decorator is a transparent pass-through (identity
     function) with zero overhead.

Usage::

    from openbot.application.use_cases._tracing import traceable


    @traceable(run_type="chain", name="triage")
    async def maybe_run_triage(ctx: PreflightContext) -> None: ...
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])

try:
    from langsmith import traceable  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    # langsmith is a declared runtime dependency (pyproject.toml); this
    # fallback is a safety net for stripped CI images only.
    def traceable(**_kwargs: object) -> Callable[[_F], _F]:  # type: ignore[misc]
        """No-op decorator when langsmith is absent."""
        del _kwargs

        def _wrap(fn: _F) -> _F:
            return fn

        return _wrap  # type: ignore[return-value]


__all__ = ["traceable"]

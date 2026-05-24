"""LangSmith + Langfuse tracing decorators — shared imports with graceful fallback.

All four use-case entry points import ``traceable`` (LangSmith) and
``observe`` (Langfuse) from here rather than from the SDKs directly so:

  1. The ``ImportError`` guards live in one place.
  2. A slim image without either SDK still boots cleanly — the fallback
     decorators are transparent pass-throughs with zero overhead.
  3. Both backends receive traces simultaneously without any coupling
     between them.

Usage::

    from openbot.application.use_cases._tracing import observe, traceable


    @observe(name="triage", capture_input=False)
    @traceable(run_type="chain", name="triage")
    async def maybe_run_triage(ctx: PreflightContext) -> None: ...

``capture_input=False`` is required on ``@observe`` because
``PreflightContext`` contains non-JSON-serialisable port objects.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])

# ── LangSmith ────────────────────────────────────────────────────────────────

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


# ── Langfuse ──────────────────────────────────────────────────────────────────

try:
    from langfuse import observe  # type: ignore[import-untyped]  # langfuse v3+
except ImportError:  # pragma: no cover
    # langfuse is a declared runtime dependency (pyproject.toml); this
    # fallback is a safety net for stripped CI images only.
    def observe(**_kwargs: object) -> Callable[[_F], _F]:  # type: ignore[misc]
        """No-op decorator when langfuse is absent."""
        del _kwargs

        def _wrap(fn: _F) -> _F:
            return fn

        return _wrap  # type: ignore[return-value]


__all__ = ["observe", "traceable"]

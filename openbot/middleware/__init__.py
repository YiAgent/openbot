"""Pre-flight middleware stack — harness spec §3 M3 / §5.1.

Slice A ships only the **framework**: types, decision protocol, runner,
and the `announce_once` helper (spec §9.2). Real gates (kill-switch /
cancel-label / fork-pr / actor-role / rate-limit / budget) land in
slices B and C; each will export a `Middleware` instance that this
package's `run_preflight` composes in order.
"""

from openbot.middleware.preflight import (
    Middleware,
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
    announce_once,
    run_preflight,
)

__all__ = [
    "Middleware",
    "MiddlewareDecision",
    "MiddlewareResult",
    "PreflightContext",
    "announce_once",
    "run_preflight",
]

"""Pre-flight middleware stack — harness spec §3 M3 / §5.1.

Slice A shipped the framework (`PreflightContext`, `MiddlewareDecision`,
`run_preflight`, `announce_once`). Slice B adds the real gates:

  - `KillSwitchMiddleware`     env-var emergency stop
  - `CancelLabelMiddleware`    repo-label drop
  - `CancelCommentMiddleware`  `@openbot stop|cancel|停|取消`
  - `BudgetMiddleware`         global hard kill + monthly soft cap
  - `RateLimitMiddleware`      chat-feature daily/hourly counters

Slice C will add `ForkPRGateMiddleware` + `ActorRoleMiddleware` + the
prompt-injection wrapper at the LLM boundary.

The actual chain order lives in `webapp._run_dispatch` so a single PR
can change it without touching the middleware modules.
"""

from openbot.middleware.budget import BudgetMiddleware
from openbot.middleware.cancel import (
    CancelCommentMiddleware,
    CancelLabelMiddleware,
    KillSwitchMiddleware,
)
from openbot.middleware.preflight import (
    Middleware,
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
    announce_once,
    run_preflight,
)
from openbot.middleware.rate_limit import RateLimitMiddleware

__all__ = [
    "BudgetMiddleware",
    "CancelCommentMiddleware",
    "CancelLabelMiddleware",
    "KillSwitchMiddleware",
    "Middleware",
    "MiddlewareDecision",
    "MiddlewareResult",
    "PreflightContext",
    "RateLimitMiddleware",
    "announce_once",
    "run_preflight",
]

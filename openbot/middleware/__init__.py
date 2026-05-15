"""Pre-flight middleware stack — harness spec §3 M3 / §5.1.

Slice A shipped the framework (`PreflightContext`, `MiddlewareDecision`,
`run_preflight`, `announce_once`). Slice B added cancel + budget +
rate-limit. Slice C adds the security gates:

  - `KillSwitchMiddleware`     env-var emergency stop          (slice B)
  - `CancelLabelMiddleware`    repo-label drop                 (slice B)
  - `CancelCommentMiddleware`  `@openbot stop|cancel|停|取消`  (slice B)
  - `ForkPRGateMiddleware`     fork PR default-deny + opt-in   (slice C)
  - `ActorRoleMiddleware`      per-feature actor allow-list    (slice C)
  - `RateLimitMiddleware`      chat-feature daily/hourly       (slice B)
  - `BudgetMiddleware`         global hard kill + soft cap     (slice B)

Slice D will replace FastAPI BackgroundTasks with a Redis Stream worker.

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
from openbot.middleware.security import ActorRoleMiddleware, ForkPRGateMiddleware

__all__ = [
    "ActorRoleMiddleware",
    "BudgetMiddleware",
    "CancelCommentMiddleware",
    "CancelLabelMiddleware",
    "ForkPRGateMiddleware",
    "KillSwitchMiddleware",
    "Middleware",
    "MiddlewareDecision",
    "MiddlewareResult",
    "PreflightContext",
    "RateLimitMiddleware",
    "announce_once",
    "run_preflight",
]

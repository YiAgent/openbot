"""Pre-flight middleware stack — harness spec §3 M3 / §5.1.

Slice A shipped the framework (`PreflightContext`, `MiddlewareDecision`,
`run_preflight`, `announce_once`). Slice B added cancel + budget +
rate-limit. Slice C added the security gates. Slice E closes the
input-side completeness gap with the entry sanitizer, the feature-
toggle gate, and the chain-end audit-start:

  - `SanitizeInputsMiddleware` byte-format gate (slice E)        ← chain head
  - `KillSwitchMiddleware`     env-var emergency stop            (slice B)
  - `FeatureToggleMiddleware`  `.openbot/config.yaml` features.* (slice E)
  - `CancelLabelMiddleware`    repo-label drop                   (slice B)
  - `CancelCommentMiddleware`  `@openbot stop|cancel|...`        (slice B)
  - `ForkPRGateMiddleware`     fork PR default-deny + opt-in     (slice C)
  - `ActorRoleMiddleware`      per-feature actor allow-list      (slice C)
  - `RateLimitMiddleware`      chat-feature daily/hourly         (slice B)
  - `BudgetMiddleware`         global hard kill + soft cap       (slice B)
  - `AuditStartMiddleware`     STARTED row, just before handler  (slice E)

Slice D replaced FastAPI BackgroundTasks with a Redis Stream worker.

The actual chain order lives in `openbot.dispatch.build_preflight_chain`
so a single PR can change it without touching the middleware modules.
"""

from openbot.middleware.audit import AuditStartMiddleware
from openbot.middleware.budget import BudgetMiddleware
from openbot.middleware.cancel import (
    CancelCommentMiddleware,
    CancelLabelMiddleware,
    KillSwitchMiddleware,
)
from openbot.middleware.feature_toggle import FeatureToggleMiddleware
from openbot.middleware.preflight import (
    Middleware,
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
    announce_once,
    run_preflight,
)
from openbot.middleware.rate_limit import RateLimitMiddleware
from openbot.middleware.sanitize import SanitizeInputsMiddleware, sanitized_event
from openbot.middleware.security import ActorRoleMiddleware, ForkPRGateMiddleware

__all__ = [
    "ActorRoleMiddleware",
    "AuditStartMiddleware",
    "BudgetMiddleware",
    "CancelCommentMiddleware",
    "CancelLabelMiddleware",
    "FeatureToggleMiddleware",
    "ForkPRGateMiddleware",
    "KillSwitchMiddleware",
    "Middleware",
    "MiddlewareDecision",
    "MiddlewareResult",
    "PreflightContext",
    "RateLimitMiddleware",
    "SanitizeInputsMiddleware",
    "announce_once",
    "run_preflight",
    "sanitized_event",
]

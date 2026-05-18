"""Phase-1 shim — re-export from infrastructure.config_loader + domain.config_schema."""

from openbot.domain.config_schema import (  # noqa: F401
    BudgetConfig,
    CancelConfig,
    EffectiveConfig,
    FeatureToggles,
    ForkPRConfig,
    ModelOverrides,
    RateLimitConfig,
    SeverityThreshold,
)
from openbot.infrastructure.config_loader import *  # noqa: F403
from openbot.infrastructure.config_loader import (  # noqa: F401
    baked_in_defaults,
    clear_cache,
    load_for_repo,
)

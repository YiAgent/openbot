"""Pure data shapes for OpenBot configuration. No I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

# Leaf-enum exception: domain → infrastructure layer violation, documented
# and deferred to Task 1.10's import-linter contract design.
from openbot.llm.router import Feature

SeverityThreshold = Literal["critical", "high", "medium", "low"]


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    """PRD §4.5 — three-tier cost cap."""

    per_task: Mapping[Feature, Decimal]
    monthly_soft_cap_usd: Decimal
    monthly_alert_at_pct: int
    global_hard_kill_usd: Decimal


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    """PRD §4.6 — chat-feature rate limits."""

    per_user_per_day: int
    per_repo_per_hour: int
    cost_cap_per_task: Decimal
    exempt_roles: frozenset[str]


@dataclass(frozen=True, slots=True)
class CancelConfig:
    """PRD §4.7 — label + comment keywords. Env kill switch lives in `Settings`."""

    label: str
    comment_keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelOverrides:
    """PRD §13 #2 — per-feature model override on top of router._PRIMARY."""

    per_feature: Mapping[Feature, str]


@dataclass(frozen=True, slots=True)
class ForkPRConfig:
    """PRD §4.8 — fork PR default-off + `/ok-to-test` opt-in."""

    run: bool
    ok_to_test_phrase: str


@dataclass(frozen=True, slots=True)
class FeatureToggles:
    """Quick gate — disable a feature without removing config."""

    triage: bool
    review: bool
    fix: bool
    chat: bool


@dataclass(frozen=True, slots=True)
class EffectiveConfig:
    """Frozen view of `.openbot/config.yaml` merged with baked-in defaults.

    `raw` retains the parsed YAML root so future modules can read fields
    not yet modeled here (e.g. `review.skip_paths`) without revisiting
    this loader.
    """

    features: FeatureToggles
    budget: BudgetConfig
    rate_limit: RateLimitConfig
    cancel: CancelConfig
    model: ModelOverrides
    fork_pr: ForkPRConfig
    severity_threshold: SeverityThreshold
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

"""`.openbot/config.yaml` loader — harness spec §3 M1.

Loads the repo-resident config via the GitHub Contents API using the
installation token from the existing GitHubAdapter, parses with
`yaml.safe_load` (CWE-502: `yaml.load` is banned in this codebase),
and returns a frozen `EffectiveConfig` ready for the Router + Pre-flight
stack to consume.

Pragmatic deviations from spec §3 M1:

  - Cache is **TTL-based** (60s) rather than `(repo, sha)`-keyed. Both
    achieve the spec acceptance ("10 calls → 1 API hit"); TTL is one
    fewer concept (no need to also fetch the default-branch sha just
    to look up the cache). Etag / `If-None-Match` is a v0.2 optimization.

  - Only the fields the harness will read in v0.1 are modeled — `budget`,
    `rate_limit` (chat), `cancel`, `model.per_feature`, `security.fork_pr`,
    `review.severity_threshold`, feature toggles. The raw parsed dict is
    retained on `EffectiveConfig.raw` for fields workflows may need
    later without changing this module.

Failure modes (none raise out of this module — fall-back to defaults):

  - File 404            → INFO log, baked-in defaults
  - 4xx / 5xx other     → WARNING log, baked-in defaults
  - JSON not YAML root  → WARNING log, baked-in defaults
  - Type coercion fails → WARNING log, baked-in defaults
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx
import yaml

from openbot.domain.config_schema import (
    BudgetConfig,
    CancelConfig,
    EffectiveConfig,
    FeatureToggles,
    ForkPRConfig,
    ModelOverrides,
    RateLimitConfig,
    SeverityThreshold,
)
from openbot.events import UnifiedEvent
from openbot.llm.router import Feature

_logger = logging.getLogger(__name__)

# Cache TTL — 60s is enough to coalesce a burst of webhooks (typical GitHub
# retry storm) without making real config changes wait long.
_CACHE_TTL_SECONDS: Final = 60.0
# Cap cache size so a misbehaving caller can't OOM the worker by feeding
# millions of distinct repo names. ~64 distinct repos per instance is far
# beyond the "1-2 repo / individual maintainer" v0.1 target.
_CACHE_MAX_ENTRIES: Final = 64


# ───────────────────────── baked-in defaults ─────────────────────────


def _default_per_task_budget() -> Mapping[Feature, Decimal]:
    return {
        Feature.TRIAGE: Decimal("0.20"),
        Feature.REVIEW: Decimal("0.50"),
        Feature.FIX: Decimal("3.00"),
        Feature.CHAT: Decimal("0.30"),
    }


def baked_in_defaults() -> EffectiveConfig:
    """The config a user with no `.openbot/config.yaml` gets.

    Mirrors `docs/prd/openbot-config-example.yaml` defaults. Exported so
    callers (tests + audit) can compare against a known baseline.
    """
    return EffectiveConfig(
        features=FeatureToggles(triage=True, review=True, fix=True, chat=True),
        budget=BudgetConfig(
            per_task=_default_per_task_budget(),
            monthly_soft_cap_usd=Decimal("100"),
            monthly_alert_at_pct=80,
            global_hard_kill_usd=Decimal("500"),
        ),
        rate_limit=RateLimitConfig(
            per_user_per_day=20,
            per_repo_per_hour=100,
            cost_cap_per_task=Decimal("0.30"),
            exempt_roles=frozenset({"owner", "collaborator"}),
        ),
        cancel=CancelConfig(
            label="cancel-openbot",
            comment_keywords=("stop", "cancel", "停", "取消"),
        ),
        model=ModelOverrides(per_feature={}),
        fork_pr=ForkPRConfig(run=False, ok_to_test_phrase="/ok-to-test"),
        severity_threshold="medium",
        raw={},
    )


# ───────────────────────── public entry point ─────────────────────────


_cache: dict[str, tuple[float, EffectiveConfig]] = {}


def _now() -> float:
    """`time.monotonic` indirection for tests."""
    return time.monotonic()


def _cache_get(repo: str) -> EffectiveConfig | None:
    entry = _cache.get(repo)
    if entry is None:
        return None
    expires_at, cfg = entry
    if expires_at <= _now():
        # Lazy eviction — cheaper than scanning the whole dict on every call.
        _cache.pop(repo, None)
        return None
    return cfg


def _cache_put(repo: str, cfg: EffectiveConfig) -> None:
    if len(_cache) >= _CACHE_MAX_ENTRIES and repo not in _cache:
        # Evict the entry closest to expiry — coarse LRU, good enough for
        # the 64-entry budget. Avoids OrderedDict + the ceremony around it.
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[repo] = (_now() + _CACHE_TTL_SECONDS, cfg)


def clear_cache() -> None:
    """Test hook — never called from production code."""
    _cache.clear()


async def load_for_repo(adapter: Any, event: UnifiedEvent) -> EffectiveConfig:
    """Load `.openbot/config.yaml` for `event.repo` (with 60s in-memory cache).

    `adapter` is duck-typed against `GitHubAdapter`: we need
    `_installation_token(event)`, `_headers(token)`, `_http`, and `_api_base`.
    Importing the concrete class would create a circular dep (adapters
    don't yet know about config).

    Never raises — every failure path returns `baked_in_defaults()` and
    logs a structured warning so ops can spot config drift in audit.
    """
    cached = _cache_get(event.repo)
    if cached is not None:
        return cached

    cfg = await _fetch_and_parse(adapter, event)
    _cache_put(event.repo, cfg)
    return cfg


async def _fetch_and_parse(adapter: Any, event: UnifiedEvent) -> EffectiveConfig:
    raw_yaml = await _fetch_yaml(adapter, event)
    if raw_yaml is None:
        return baked_in_defaults()
    try:
        parsed = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        _logger.warning(
            "config_yaml_parse_failed",
            extra={"repo": event.repo, "reason": str(exc)[:200]},
        )
        return baked_in_defaults()
    if not isinstance(parsed, dict):
        _logger.warning(
            "config_yaml_root_not_mapping",
            extra={"repo": event.repo, "got": type(parsed).__name__},
        )
        return baked_in_defaults()
    return _coerce(parsed, repo=event.repo)


async def _fetch_yaml(adapter: Any, event: UnifiedEvent) -> str | None:
    """Pull `.openbot/config.yaml` over the Contents API.

    Returns:
        UTF-8 decoded text on success, `None` on 404 / auth error / 5xx.
    """
    if event.installation_id is None:
        _logger.info(
            "config_skip_no_installation",
            extra={"repo": event.repo, "delivery_id": event.delivery_id},
        )
        return None
    try:
        token = await adapter._installation_token(event)
    except Exception:
        _logger.exception(
            "config_installation_token_failed",
            extra={"repo": event.repo},
        )
        return None

    url = f"{adapter._api_base}/repos/{event.repo}/contents/.openbot/config.yaml"
    headers = adapter._headers(token.token)
    try:
        response = await adapter._http_client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        _logger.warning(
            "config_fetch_http_error",
            extra={"repo": event.repo, "reason": f"{type(exc).__name__}: {exc}"},
        )
        return None

    if response.status_code == 404:
        _logger.info("config_yaml_absent", extra={"repo": event.repo})
        return None
    if response.status_code >= 400:
        _logger.warning(
            "config_yaml_fetch_failed",
            extra={"repo": event.repo, "status": response.status_code},
        )
        return None

    try:
        payload = response.json()
    except ValueError:
        _logger.warning(
            "config_yaml_response_not_json",
            extra={"repo": event.repo, "status": response.status_code},
        )
        return None

    encoded = payload.get("content") or ""
    encoding = payload.get("encoding") or "base64"
    if encoding != "base64":
        _logger.warning(
            "config_yaml_unexpected_encoding",
            extra={"repo": event.repo, "encoding": encoding},
        )
        return None
    try:
        return base64.b64decode(encoded, validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        _logger.warning(
            "config_yaml_decode_failed",
            extra={"repo": event.repo, "reason": str(exc)[:200]},
        )
        return None


# ───────────────────────── coercion ─────────────────────────


def _coerce(parsed: Mapping[str, Any], *, repo: str) -> EffectiveConfig:
    """Merge baked-in defaults with user YAML.

    Each section is coerced independently — a malformed `budget` block
    falls back to budget defaults without losing valid `cancel` overrides.
    """
    base = baked_in_defaults()
    try:
        return EffectiveConfig(
            features=_coerce_features(parsed, base.features),
            budget=_coerce_budget(parsed, base.budget, repo=repo),
            rate_limit=_coerce_rate_limit(parsed, base.rate_limit, repo=repo),
            cancel=_coerce_cancel(parsed, base.cancel),
            model=_coerce_model(parsed, base.model, repo=repo),
            fork_pr=_coerce_fork_pr(parsed, base.fork_pr),
            severity_threshold=_coerce_severity(parsed, base.severity_threshold),
            raw=parsed,
        )
    except Exception:
        # Catch-all on the assembly step itself; per-section errors are
        # already handled below. This guards against future field additions
        # that introduce unexpected interactions.
        _logger.exception("config_coerce_failed", extra={"repo": repo})
        return base


def _coalesce(value: Any, default: Any) -> Any:
    """Return `value` when not None, else `default`.

    Crucially distinguishes `0` / `Decimal('0')` / `False` from "absent".
    The previous `value or default` pattern silently restored the default
    when a maintainer set a budget cap to `0` to disable spend or a rate
    limit to `0` to block the feature — which is the exact opposite of
    what the maintainer wanted. Use this helper for every config field
    where 0 is a meaningful "off" value.
    """
    return value if value is not None else default


def _coerce_features(parsed: Mapping[str, Any], default: FeatureToggles) -> FeatureToggles:
    section = parsed.get("features") or {}
    if not isinstance(section, Mapping):
        return default
    return FeatureToggles(
        triage=bool(section.get("triage", default.triage)),
        review=bool(section.get("review", default.review)),
        fix=bool(section.get("fix", default.fix)),
        chat=bool(section.get("chat", default.chat)),
    )


def _coerce_budget(parsed: Mapping[str, Any], default: BudgetConfig, *, repo: str) -> BudgetConfig:
    section = parsed.get("budget")
    if not isinstance(section, Mapping):
        return default
    per_task_raw = section.get("per_task") or {}
    per_task: dict[Feature, Decimal] = dict(default.per_task)
    if isinstance(per_task_raw, Mapping):
        for feature in Feature:
            if feature.value in per_task_raw:
                amount = _to_decimal(
                    per_task_raw[feature.value], repo=repo, field=f"budget.per_task.{feature.value}"
                )
                if amount is not None:
                    per_task[feature] = amount
    return BudgetConfig(
        per_task=per_task,
        monthly_soft_cap_usd=_coalesce(
            _to_decimal(
                section.get("monthly_soft_cap_usd"),
                repo=repo,
                field="budget.monthly_soft_cap_usd",
            ),
            default.monthly_soft_cap_usd,
        ),
        monthly_alert_at_pct=_coalesce(
            _to_int(
                section.get("monthly_alert_at_pct"),
                repo=repo,
                field="budget.monthly_alert_at_pct",
            ),
            default.monthly_alert_at_pct,
        ),
        global_hard_kill_usd=_coalesce(
            _to_decimal(
                section.get("global_hard_kill_usd"),
                repo=repo,
                field="budget.global_hard_kill_usd",
            ),
            default.global_hard_kill_usd,
        ),
    )


def _coerce_rate_limit(
    parsed: Mapping[str, Any], default: RateLimitConfig, *, repo: str
) -> RateLimitConfig:
    # Chat-feature rate limits live under `chat.rate_limit` per PRD §4.6 example.
    chat_section = parsed.get("chat")
    if not isinstance(chat_section, Mapping):
        return default
    section = chat_section.get("rate_limit")
    if not isinstance(section, Mapping):
        return default
    roles_raw = section.get("exempt_roles")
    if isinstance(roles_raw, list):
        exempt_roles = frozenset(str(r) for r in roles_raw if isinstance(r, str))
    else:
        exempt_roles = default.exempt_roles
    return RateLimitConfig(
        per_user_per_day=_coalesce(
            _to_int(
                section.get("per_user_per_day"),
                repo=repo,
                field="chat.rate_limit.per_user_per_day",
            ),
            default.per_user_per_day,
        ),
        per_repo_per_hour=_coalesce(
            _to_int(
                section.get("per_repo_per_hour"),
                repo=repo,
                field="chat.rate_limit.per_repo_per_hour",
            ),
            default.per_repo_per_hour,
        ),
        cost_cap_per_task=_coalesce(
            _to_decimal(
                section.get("cost_cap_per_task"),
                repo=repo,
                field="chat.rate_limit.cost_cap_per_task",
            ),
            default.cost_cap_per_task,
        ),
        exempt_roles=exempt_roles,
    )


def _coerce_cancel(parsed: Mapping[str, Any], default: CancelConfig) -> CancelConfig:
    section = parsed.get("cancel")
    if not isinstance(section, Mapping):
        return default
    label = section.get("label")
    keywords_raw = section.get("comment_keywords")
    if isinstance(keywords_raw, list):
        keywords = tuple(str(k) for k in keywords_raw if isinstance(k, str))
    else:
        keywords = default.comment_keywords
    return CancelConfig(
        label=str(label) if isinstance(label, str) and label else default.label,
        comment_keywords=keywords or default.comment_keywords,
    )


def _coerce_model(
    parsed: Mapping[str, Any], default: ModelOverrides, *, repo: str
) -> ModelOverrides:
    section = parsed.get("model")
    if not isinstance(section, Mapping):
        return default
    per_feature_raw = section.get("per_feature") or {}
    if not isinstance(per_feature_raw, Mapping):
        return default
    per_feature: dict[Feature, str] = dict(default.per_feature)
    for feature in Feature:
        value = per_feature_raw.get(feature.value)
        if isinstance(value, str) and value:
            per_feature[feature] = value
        elif value is not None:
            _logger.warning(
                "config_model_override_bad_type",
                extra={"repo": repo, "feature": feature.value, "got": type(value).__name__},
            )
    return ModelOverrides(per_feature=per_feature)


def _coerce_fork_pr(parsed: Mapping[str, Any], default: ForkPRConfig) -> ForkPRConfig:
    security = parsed.get("security")
    if not isinstance(security, Mapping):
        return default
    section = security.get("fork_pr")
    if not isinstance(section, Mapping):
        return default
    phrase = section.get("ok_to_test_phrase")
    return ForkPRConfig(
        run=bool(section.get("run", default.run)),
        ok_to_test_phrase=str(phrase)
        if isinstance(phrase, str) and phrase
        else default.ok_to_test_phrase,
    )


def _coerce_severity(parsed: Mapping[str, Any], default: SeverityThreshold) -> SeverityThreshold:
    review = parsed.get("review")
    if not isinstance(review, Mapping):
        return default
    threshold = review.get("severity_threshold")
    if threshold in ("critical", "high", "medium", "low"):
        return threshold  # type: ignore[return-value]
    return default


def _to_decimal(value: Any, *, repo: str, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        _logger.warning(
            "config_bad_decimal",
            extra={"repo": repo, "field": field, "raw": str(value)[:64]},
        )
        return None


def _to_int(value: Any, *, repo: str, field: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        _logger.warning(
            "config_bad_int",
            extra={"repo": repo, "field": field, "raw": str(value)[:64]},
        )
        return None

"""Single source of truth for OpenBot eval tunables.

Mirrors :mod:`openbot.config`'s ``pydantic-settings`` style so the eval
side never re-invents env parsing. Modules import :func:`get_eval_config`
(or use the re-exported env-name constants where they need a literal
``os.environ.get(...)`` call site, e.g. for chain-resolution semantics
like ``override → env → fallback``).

What lives here:
  - **Env var names** as ``Final[str]`` constants — re-exported for
    tests and dynamic call-site reads.
  - A **typed Settings tree** (one section per concern) with validated
    defaults. Built on ``pydantic-settings`` so invalid env values fail
    loud at startup instead of silently using a wrong number.
  - The ``@lru_cache``'d :func:`get_eval_config` accessor — tests call
    ``get_eval_config.cache_clear()`` after ``monkeypatch.setenv`` so
    the next read re-parses the env.

What intentionally does **not** live here:
  - Prompt bodies, paper-pinned dimensions / weights, judge VERSION
    constants — those are part of each scorer's *contract* docstring
    ("locked surface, do not edit without bumping VERSION") and need
    to stay co-located with the prompt they're locked against.
  - Implementation details: regex patterns, shell installer scripts,
    process-scoped state (session ids, lock dicts, label keys).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

# ─── Re-exported env-var names ──────────────────────────────────────────────
# Sites that resolve chains (``override → env → fallback``) still read
# ``os.environ.get(NAME)`` directly because pydantic's snapshot semantics
# don't fit per-call overrides. Tests use these to ``monkeypatch.setenv``.

# String defaults used in per-call fallback chains (resolve_model,
# resolve_judge_model). Declared here so the literal lives once; pydantic
# Sections below reference the same constant.
DEEPAGENTS_MODEL_FALLBACK: Final[str] = "anthropic:claude-sonnet-4-6"
JUDGE_MODEL_DEFAULT: Final[str] = "anthropic:claude-opus-4-7"

DEEPAGENTS_MODEL_ENV: Final[str] = "OPENBOT_DEEPAGENTS_MODEL"
DEEPAGENTS_MODEL_CALL_LIMIT_ENV: Final[str] = "OPENBOT_DEEPAGENTS_MODEL_CALL_LIMIT"
DEEPAGENTS_TOOL_CALL_LIMIT_ENV: Final[str] = "OPENBOT_DEEPAGENTS_TOOL_CALL_LIMIT"
DEEPAGENTS_RECURSION_LIMIT_ENV: Final[str] = "OPENBOT_DEEPAGENTS_RECURSION_LIMIT"
DEEPAGENTS_MODEL_TIMEOUT_S_ENV: Final[str] = "OPENBOT_DEEPAGENTS_MODEL_TIMEOUT_S"
DEEPAGENTS_MODEL_MAX_RETRIES_ENV: Final[str] = "OPENBOT_DEEPAGENTS_MODEL_MAX_RETRIES"
DEEPAGENTS_MAX_OUTPUT_TOKENS_ENV: Final[str] = "OPENBOT_DEEPAGENTS_MAX_OUTPUT_TOKENS"
DEEPAGENTS_THINKING_LEVEL_ENV: Final[str] = "OPENBOT_DEEPAGENTS_THINKING_LEVEL"

# Anthropic-protocol ``thinking={"type": "enabled", "budget_tokens": N}``
# budgets per discrete level. The strings mirror Anthropic's documented
# thinking tiers; numeric budgets are conservative defaults that fit
# inside our 32k max_output_tokens cap without crowding the answer body.
# ``off`` disables thinking entirely (most compatible with non-Anthropic
# gateways that don't honour the field). Mapping is consulted by
# :func:`evals.common.deepagents_baseline.get_thinking_budget_tokens`.
THINKING_LEVEL_BUDGETS: Final[dict[str, int]] = {
    "off": 0,
    "low": 1024,
    "medium": 5000,
    "high": 10000,
}

JUDGE_MODEL_ENV: Final[str] = "OPENBOT_JUDGE_MODEL_ID"
REVIEW_JUDGE_MODEL_ENV: Final[str] = "OPENBOT_REVIEW_JUDGE_MODEL_ID"
SWE_QA_JUDGE_MODEL_ENV: Final[str] = "OPENBOT_SWE_QA_JUDGE_MODEL"

SANDBOX_BACKEND_ENV: Final[str] = "OPENBOT_SANDBOX_BACKEND"
SANDBOX_BACKENDS_SUPPORTED: Final[tuple[str, ...]] = ("docker", "modal", "daytona")
DAYTONA_IMAGE_ENV: Final[str] = "OPENBOT_DAYTONA_IMAGE"
MODAL_IMAGE_ENV: Final[str] = "OPENBOT_MODAL_IMAGE"
MODAL_APP_ENV: Final[str] = "OPENBOT_MODAL_APP"

LANGSMITH_EVAL_PROJECT_ENV: Final[str] = "LANGSMITH_PROJECT_EVAL"

PREDICTIONS_OUTPUT_DIR_ENV: Final[str] = "OPENBOT_PREDICTIONS_DIR"


SandboxKind = Literal["docker", "modal", "daytona"]


def _normalize_kind(value: object) -> object:
    """Lowercase + strip the backend env value before Literal validation.

    Keeps the existing "OPENBOT_SANDBOX_BACKEND=DOCKER" ergonomic without
    losing pydantic's strict "unknown value → ValidationError" gate.
    """
    if isinstance(value, str):
        return value.strip().lower()
    return value


_NormalizedSandboxKind = Annotated[SandboxKind, BeforeValidator(_normalize_kind)]


# ─── Section settings ───────────────────────────────────────────────────────
# Each section is its own ``BaseSettings`` so per-field ``validation_alias``
# binds straight to the OPENBOT_* env name (no need for a class-wide prefix
# that would force renaming). All sections are frozen, ignore extras, and
# accept ``.env`` overrides — same shape as :class:`openbot.config.Settings`.

_SETTINGS_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
    frozen=True,
)


class DeepAgentsSettings(BaseSettings):
    """Per-sample runaway-guard budgets + HTTP client knobs.

    Defaults chosen from observed smoke runs — see
    :mod:`evals.common.deepagents_baseline` module docstring for the
    empirical reasoning behind each value.
    """

    model_config = _SETTINGS_CONFIG

    model: str = Field(
        default=DEEPAGENTS_MODEL_FALLBACK,
        validation_alias=DEEPAGENTS_MODEL_ENV,
    )
    model_call_limit: PositiveInt = Field(
        default=100, validation_alias=DEEPAGENTS_MODEL_CALL_LIMIT_ENV
    )
    tool_call_limit: PositiveInt = Field(
        default=200, validation_alias=DEEPAGENTS_TOOL_CALL_LIMIT_ENV
    )
    recursion_limit: PositiveInt = Field(
        default=400, validation_alias=DEEPAGENTS_RECURSION_LIMIT_ENV
    )
    model_timeout_s: PositiveInt = Field(
        default=90, validation_alias=DEEPAGENTS_MODEL_TIMEOUT_S_ENV
    )
    model_max_retries: PositiveInt = Field(
        default=3, validation_alias=DEEPAGENTS_MODEL_MAX_RETRIES_ENV
    )
    # 100k: empirically, 16k was still too tight for thinking-style models
    # on review samples where chain-of-thought consumed the entire budget
    # before the final findings JSON could be emitted.
    max_output_tokens: PositiveInt = Field(
        default=100_000, validation_alias=DEEPAGENTS_MAX_OUTPUT_TOKENS_ENV
    )
    # Anthropic-protocol extended-thinking level. Maps to ``budget_tokens``
    # via :data:`THINKING_LEVEL_BUDGETS`. ``medium`` gives the model
    # ~5k tokens of structured reasoning before its final answer — enough
    # to plan multi-step investigations on review / fix without runaway
    # chain-of-thought (which is what the convergence middlewares and
    # max_output_tokens cap also defend against). ``off`` for gateways
    # that don't honour the field.
    thinking_level: Literal["off", "low", "medium", "high"] = Field(
        default="medium", validation_alias=DEEPAGENTS_THINKING_LEVEL_ENV
    )


class JudgeSettings(BaseSettings):
    """LLM-as-judge client defaults shared across every scorer."""

    model_config = _SETTINGS_CONFIG

    model_id: str = Field(
        default=JUDGE_MODEL_DEFAULT,
        validation_alias=JUDGE_MODEL_ENV,
    )
    # Per-scorer overrides remain ``None`` here when their env is unset.
    # Scorer modules read them through
    # :func:`evals.common.judge_client.resolve_judge_model`, which falls
    # back to ``model_id`` when None.
    review_model_id: str | None = Field(default=None, validation_alias=REVIEW_JUDGE_MODEL_ENV)
    swe_qa_model_id: str | None = Field(default=None, validation_alias=SWE_QA_JUDGE_MODEL_ENV)
    # Per-scorer modules still re-export their own ``*_TEMPERATURE`` /
    # ``*_MAX_TOKENS`` constants for paper-faithfulness audits; the
    # numeric values originate here.
    temperature: float = 0.0
    max_tokens: PositiveInt = 4096


class SandboxSettings(BaseSettings):
    """Per-sample sandbox backend selector + shared image defaults."""

    model_config = _SETTINGS_CONFIG

    backend: _NormalizedSandboxKind = Field(default="daytona", validation_alias=SANDBOX_BACKEND_ENV)
    # docker/modal/daytona all start from ``python:3.11-slim`` (~120 MB,
    # git installed on first use) for cross-backend parity.
    default_image: str = "python:3.11-slim"
    default_run_timeout_s: PositiveInt = 600
    # Per-backend image overrides — ``None`` means "use default_image".
    daytona_image: str | None = Field(default=None, validation_alias=DAYTONA_IMAGE_ENV)
    modal_image: str | None = Field(default=None, validation_alias=MODAL_IMAGE_ENV)
    modal_app: str = Field(default="openbot-eval-sandbox", validation_alias=MODAL_APP_ENV)


class LangSmithSettings(BaseSettings):
    """LangSmith trace routing — only the OpenBot-owned eval project env."""

    model_config = _SETTINGS_CONFIG

    eval_project: str | None = Field(default=None, validation_alias=LANGSMITH_EVAL_PROJECT_ENV)


class PredictionsSettings(BaseSettings):
    """Prediction JSONL output directory."""

    model_config = _SETTINGS_CONFIG

    output_dir: Path = Field(
        default=Path("evals/outputs"), validation_alias=PREDICTIONS_OUTPUT_DIR_ENV
    )


class EvalSettings(BaseSettings):
    """Aggregate root — one frozen instance per process via :func:`get_eval_config`."""

    model_config = _SETTINGS_CONFIG

    deepagents: DeepAgentsSettings = Field(default_factory=DeepAgentsSettings)
    judge: JudgeSettings = Field(default_factory=JudgeSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    langsmith: LangSmithSettings = Field(default_factory=LangSmithSettings)
    predictions: PredictionsSettings = Field(default_factory=PredictionsSettings)


@lru_cache
def get_eval_config() -> EvalSettings:
    """Process-wide cached eval config.

    Tests that mutate the environment via ``monkeypatch.setenv`` must
    call ``get_eval_config.cache_clear()`` so the next read re-parses
    the new env. The autouse env-isolation fixture in
    ``tests/conftest.py`` does this for them.
    """
    return EvalSettings()

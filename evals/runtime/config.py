"""Single source of truth for OpenBot eval tunables.

All eval-owned configuration: datasets, judge models, LangSmith routing,
and prediction output. Sandbox lifecycle and model routing are OpenBot product
concerns — they live in ``openbot.core.settings``, not here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final

from pydantic import BaseModel, Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

# ─── Re-exported env-var names ──────────────────────────────────────────────

JUDGE_MODEL_DEFAULT: Final[str] = "claude-opus-4-7"
"""Bare Anthropic model id — fed straight into :class:`ChatAnthropic`.

The ``OPENBOT_*_JUDGE_MODEL*`` env vars must also be bare ids (e.g.
``claude-opus-4-7``); the older ``anthropic:claude-opus-4-7`` /
``anthropic/claude-opus-4-7`` deepagents-style prefixes are no longer
stripped — pass them and ``ChatAnthropic`` will 404.
"""

JUDGE_MODEL_ENV: Final[str] = "OPENBOT_JUDGE_MODEL_ID"
REVIEW_JUDGE_MODEL_ENV: Final[str] = "OPENBOT_REVIEW_JUDGE_MODEL_ID"
SWE_QA_JUDGE_MODEL_ENV: Final[str] = "OPENBOT_SWE_QA_JUDGE_MODEL"

LANGSMITH_EVAL_PROJECT_ENV: Final[str] = "LANGSMITH_PROJECT_EVAL"

PREDICTIONS_OUTPUT_DIR_ENV: Final[str] = "OPENBOT_PREDICTIONS_DIR"

# ─── Settings ───────────────────────────────────────────────────────────────

_SETTINGS_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
    frozen=True,
    # Treat empty env-var values as unset so silent ``OPENBOT_*=`` lines in
    # Doppler / .env don't shadow the field defaults. Without this, the old
    # resolver had to guard ``""`` explicitly; now ``JudgeSettings`` handles
    # it uniformly and ``s.review_model_id or s.model_id`` always lands on
    # a real id.
    env_ignore_empty=True,
)


class JudgeSettings(BaseSettings):
    """LLM-as-judge client defaults shared across every scorer."""

    model_config = _SETTINGS_CONFIG

    model_id: str = Field(
        default=JUDGE_MODEL_DEFAULT,
        validation_alias=JUDGE_MODEL_ENV,
    )
    review_model_id: str | None = Field(default=None, validation_alias=REVIEW_JUDGE_MODEL_ENV)
    swe_qa_model_id: str | None = Field(default=None, validation_alias=SWE_QA_JUDGE_MODEL_ENV)
    temperature: float = 0.0
    max_tokens: PositiveInt = 4096


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


class _DatasetSlot(BaseModel):
    """Immutable descriptor for one eval suite's canonical dataset."""

    model_config = {"frozen": True}

    dataset_version: str
    hf_dataset: str = ""
    dataset_source: str = ""


class CatalogSettings(BaseModel):
    """Locked dataset & solver-family identifiers for all eval suites."""

    model_config = {"frozen": True}

    # ── solver family ────────────────────────────────────────────────────────
    solver_family_baseline: str = "openbot_agent"

    # ── dataset slots ────────────────────────────────────────────────────────
    review: _DatasetSlot = _DatasetSlot(
        dataset_version="martian_2026w20",
    )
    chat: _DatasetSlot = _DatasetSlot(
        dataset_version="chat_swe_qa_pro_v1",
        hf_dataset="TIGER-Lab/SWE-QA-Pro-Bench",
        dataset_source=(
            "langsmith:chat_swe_qa_pro_v1 (mirror of huggingface:TIGER-Lab/SWE-QA-Pro-Bench)"
        ),
    )
    fix: _DatasetSlot = _DatasetSlot(
        dataset_version="fix_swe_bench_verified",
        hf_dataset="princeton-nlp/SWE-bench_Verified",
        dataset_source="huggingface:princeton-nlp/SWE-bench_Verified",
    )
    swt: _DatasetSlot = _DatasetSlot(
        dataset_version="test_swt_bench_verified",
        hf_dataset="eth-sri/SWT-bench_Verified_bm25_27k_zsb",
    )

    # ── LangSmith feedback keys ──────────────────────────────────────────────
    swe_export_feedback_key: str = "swe_export_ok"
    swt_export_feedback_key: str = "swt_export_ok"
    swe_harness_feedback_key: str = "swe_bench_pass_at_1"
    swt_harness_feedback_key: str = "swt_bench_pass_at_1"
    unit_feedback_config: dict[str, object] = Field(
        default_factory=lambda: {"type": "continuous", "min": 0.0, "max": 1.0}
    )


class EvalSettings(BaseSettings):
    """Aggregate root — one frozen instance per process via :func:`get_eval_config`."""

    model_config = _SETTINGS_CONFIG

    judge: JudgeSettings = Field(default_factory=JudgeSettings)
    langsmith: LangSmithSettings = Field(default_factory=LangSmithSettings)
    predictions: PredictionsSettings = Field(default_factory=PredictionsSettings)
    catalog: CatalogSettings = Field(default_factory=CatalogSettings)


@lru_cache
def get_eval_config() -> EvalSettings:
    """Process-wide cached eval config.

    Tests that mutate the environment via ``monkeypatch.setenv`` must
    call ``get_eval_config.cache_clear()`` so the next read re-parses
    the new env.
    """
    return EvalSettings()

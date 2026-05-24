"""Redirect stub — canonical config is :mod:`evals.runtime.config`.

All importers should update to:
    from evals.runtime.config import get_eval_config, JudgeSettings, ...
"""

from evals.runtime.config import (  # noqa: F401
    JUDGE_MODEL_DEFAULT,
    JUDGE_MODEL_ENV,
    LANGSMITH_EVAL_PROJECT_ENV,
    PREDICTIONS_OUTPUT_DIR_ENV,
    REVIEW_JUDGE_MODEL_ENV,
    SWE_QA_JUDGE_MODEL_ENV,
    CatalogSettings,
    EvalSettings,
    JudgeSettings,
    LangSmithSettings,
    PredictionsSettings,
    get_eval_config,
)

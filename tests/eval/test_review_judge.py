"""Contract tests for the Martian-compatible review judge surface."""

from __future__ import annotations

from evals.scorers import review_judge


def test_format_golden_returns_raw_body_or_comment() -> None:
    """v4 alignment: upstream substitutes ``gc["comment"]`` directly — no headers."""
    assert review_judge.format_golden({"severity": "high", "body": "Use body"}) == "Use body"
    assert review_judge.format_golden({"comment": "Use comment"}) == "Use comment"
    assert review_judge.format_golden({}) == ""


def test_format_candidate_returns_raw_body_or_comment() -> None:
    """v4 alignment: upstream feeds the candidate text in directly — no Location/Severity headers."""
    assert (
        review_judge.format_candidate(
            {"file": "src/app.py", "line": 12, "severity": "medium", "comment": "Bug"}
        )
        == "Bug"
    )
    assert review_judge.format_candidate({"body": "Body wins over comment", "comment": "x"}) == (
        "Body wins over comment"
    )


def test_system_prompt_is_separated_from_user_prompt() -> None:
    """v4: system message is its own message; do not merge into the user prompt."""
    assert review_judge.MARTIAN_JUDGE_SYSTEM_PROMPT == (
        "You are a precise code review evaluator. Always respond with valid JSON."
    )
    assert "precise code review evaluator" not in review_judge.MARTIAN_JUDGE_PROMPT
    assert review_judge.MARTIAN_JUDGE_PROMPT.startswith("You are evaluating AI code review tools.")


def test_judge_version_pin() -> None:
    """Bumping the prompt or sampling surface must bump ``MARTIAN_JUDGE_VERSION``."""
    assert review_judge.MARTIAN_JUDGE_VERSION == 4
    assert review_judge.MARTIAN_JUDGE_TEMPERATURE == 0.0


def test_judge_settings_prefers_per_judge_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Per-judge env var beats the shared default when both are set."""
    from evals.runtime import config

    monkeypatch.setenv(config.JUDGE_MODEL_ENV, "shared-default")
    monkeypatch.setenv(config.REVIEW_JUDGE_MODEL_ENV, "per-judge-override")
    config.get_eval_config.cache_clear()
    s = config.get_eval_config().judge
    assert (s.review_model_id or s.model_id) == "per-judge-override"


def test_judge_settings_falls_through_to_shared(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Without a per-judge override, the shared JUDGE_MODEL_ENV wins."""
    from evals.runtime import config

    monkeypatch.setenv(config.JUDGE_MODEL_ENV, "mimo-v2.5")
    monkeypatch.delenv(config.REVIEW_JUDGE_MODEL_ENV, raising=False)
    config.get_eval_config.cache_clear()
    s = config.get_eval_config().judge
    assert (s.review_model_id or s.model_id) == "mimo-v2.5"


def test_judge_settings_falls_back_to_hardcoded_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No env vars → the hardcoded last-resort fallback keeps imports working."""
    from evals.runtime import config

    monkeypatch.delenv(config.JUDGE_MODEL_ENV, raising=False)
    monkeypatch.delenv(config.REVIEW_JUDGE_MODEL_ENV, raising=False)
    config.get_eval_config.cache_clear()
    s = config.get_eval_config().judge
    assert (s.review_model_id or s.model_id) == config.JUDGE_MODEL_DEFAULT


def test_judge_settings_treats_empty_env_var_as_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Empty-string env vars must NOT override — silent empties in Doppler are common.

    ``JudgeSettings`` validates an empty string for ``review_model_id`` (typed
    ``str | None``) as ``None``, and an empty ``model_id`` falls back to the
    hardcoded default; the ``or`` chain therefore lands on
    :data:`config.JUDGE_MODEL_DEFAULT`, matching the previous resolver
    behaviour without any custom emptiness handling.
    """
    from evals.runtime import config

    monkeypatch.setenv(config.JUDGE_MODEL_ENV, "")
    monkeypatch.setenv(config.REVIEW_JUDGE_MODEL_ENV, "")
    config.get_eval_config.cache_clear()
    s = config.get_eval_config().judge
    assert (s.review_model_id or s.model_id) == config.JUDGE_MODEL_DEFAULT

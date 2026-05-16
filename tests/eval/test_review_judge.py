"""Contract tests for the Martian-compatible review judge surface."""

from __future__ import annotations

from evals.scorers import review_judge


def test_format_golden_prefers_body_then_comment() -> None:
    assert review_judge.format_golden({"severity": "high", "body": "Use body"}) == (
        "Severity: high\nComment: Use body"
    )
    assert review_judge.format_golden({"comment": "Use comment"}) == "Comment: Use comment"


def test_format_candidate_includes_location_severity_and_comment() -> None:
    assert (
        review_judge.format_candidate(
            {"file": "src/app.py", "line": 12, "severity": "medium", "comment": "Bug"}
        )
        == "Location: src/app.py:12\nSeverity: medium\nComment: Bug"
    )


def test_parse_judge_reply_returns_safe_miss_for_invalid_json() -> None:
    assert review_judge._parse_judge_reply("not json") == {
        "match": False,
        "confidence": 0.0,
        "reasoning": "unparseable: not json",
    }


def test_resolve_judge_model_prefers_per_judge_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Per-judge env var beats the shared default when both are set."""
    from evals.common.judge_client import resolve_judge_model

    monkeypatch.setenv("OPENBOT_JUDGE_MODEL_ID", "shared-default")
    monkeypatch.setenv("OPENBOT_REVIEW_JUDGE_MODEL_ID", "per-judge-override")
    assert (
        resolve_judge_model(per_judge_env="OPENBOT_REVIEW_JUDGE_MODEL_ID") == "per-judge-override"
    )


def test_resolve_judge_model_falls_through_to_shared(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Without a per-judge override, the shared OPENBOT_JUDGE_MODEL_ID wins."""
    from evals.common.judge_client import resolve_judge_model

    monkeypatch.setenv("OPENBOT_JUDGE_MODEL_ID", "anthropic:mimo-v2.5")
    monkeypatch.delenv("OPENBOT_REVIEW_JUDGE_MODEL_ID", raising=False)
    assert (
        resolve_judge_model(per_judge_env="OPENBOT_REVIEW_JUDGE_MODEL_ID") == "anthropic:mimo-v2.5"
    )


def test_resolve_judge_model_falls_back_to_hardcoded_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No env vars → the hardcoded last-resort fallback keeps imports working."""
    from evals.common.judge_client import _DEFAULT_JUDGE_MODEL_ID, resolve_judge_model

    monkeypatch.delenv("OPENBOT_JUDGE_MODEL_ID", raising=False)
    monkeypatch.delenv("OPENBOT_REVIEW_JUDGE_MODEL_ID", raising=False)
    assert (
        resolve_judge_model(per_judge_env="OPENBOT_REVIEW_JUDGE_MODEL_ID")
        == _DEFAULT_JUDGE_MODEL_ID
    )


def test_resolve_judge_model_treats_empty_env_var_as_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Empty-string env vars must NOT override — silent empties in Doppler are common."""
    from evals.common.judge_client import _DEFAULT_JUDGE_MODEL_ID, resolve_judge_model

    monkeypatch.setenv("OPENBOT_JUDGE_MODEL_ID", "")
    monkeypatch.setenv("OPENBOT_REVIEW_JUDGE_MODEL_ID", "")
    assert (
        resolve_judge_model(per_judge_env="OPENBOT_REVIEW_JUDGE_MODEL_ID")
        == _DEFAULT_JUDGE_MODEL_ID
    )

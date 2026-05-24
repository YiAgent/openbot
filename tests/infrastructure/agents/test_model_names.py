# tests/infrastructure/agents/test_model_names.py
from openbot.infrastructure.agents.model_names import display_name, normalize_for_langchain


def test_normalize_slash_to_colon() -> None:
    assert normalize_for_langchain("anthropic/GLM-5.1") == "anthropic:GLM-5.1"


def test_normalize_already_colon_idempotent() -> None:
    assert normalize_for_langchain("anthropic:GLM-5.1") == "anthropic:GLM-5.1"


def test_normalize_bare_name_unchanged() -> None:
    assert normalize_for_langchain("GLM-5.1") == "GLM-5.1"


def test_normalize_openai_slash() -> None:
    assert normalize_for_langchain("openai/gpt-5-mini") == "openai:gpt-5-mini"


def test_display_name_strips_prefix() -> None:
    assert display_name("anthropic:GLM-5.1") == "GLM-5.1"


def test_display_name_bare_idempotent() -> None:
    assert display_name("GLM-5.1") == "GLM-5.1"


def test_display_name_strips_only_first_segment() -> None:
    # openai:org:model → org:model
    assert display_name("openai:org:model") == "org:model"

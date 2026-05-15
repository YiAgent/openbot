"""Unit tests for the dual-project routing in evals.common.langsmith — PRD §13.2."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from evals.common import langsmith as ls

# ─── is_public_dataset: manifest reading ─────────────────────────────────────


def test_public_true_in_manifest_returns_true(tmp_path: Path) -> None:
    (tmp_path / "martian_review_v1.yaml").write_text("name: martian\npublic: true\n")
    assert ls.is_public_dataset("martian_review_v1", manifest_dir=tmp_path) is True


def test_public_false_in_manifest_returns_false(tmp_path: Path) -> None:
    (tmp_path / "internal_prs_v1.yaml").write_text("name: internal_prs\npublic: false\n")
    assert ls.is_public_dataset("internal_prs_v1", manifest_dir=tmp_path) is False


def test_missing_manifest_fails_safe_to_internal(tmp_path: Path) -> None:
    """A missing manifest must default to internal, never accidentally public."""
    assert ls.is_public_dataset("nonexistent_v1", manifest_dir=tmp_path) is False


def test_missing_public_field_defaults_to_internal(tmp_path: Path) -> None:
    (tmp_path / "broken_v1.yaml").write_text("name: broken\n# public field forgotten\n")
    assert ls.is_public_dataset("broken_v1", manifest_dir=tmp_path) is False


def test_yes_and_one_also_count_as_public(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("public: yes\n")
    (tmp_path / "b.yaml").write_text("public: 1\n")
    assert ls.is_public_dataset("a", manifest_dir=tmp_path) is True
    assert ls.is_public_dataset("b", manifest_dir=tmp_path) is True


# ─── init_client_for_dataset: routing to internal vs public ──────────────────


def test_internal_dataset_picks_internal_project_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An internal-dataset run hits LANGSMITH_PROJECT_INTERNAL (PRD §13.2)."""
    (tmp_path / "internal_v1.yaml").write_text("public: false\n")
    monkeypatch.setattr(ls, "_MANIFEST_DIR", tmp_path)
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setenv("LANGSMITH_PROJECT_INTERNAL", "openbot-eval-internal")
    monkeypatch.delenv("LANGSMITH_PROJECT_PUBLIC", raising=False)

    with patch.object(ls, "init_client", return_value=MagicMock()) as init_spy:
        ls.init_client_for_dataset("internal_v1")
    init_spy.assert_called_once_with(public=False)


def test_public_dataset_picks_public_project_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A public-dataset run hits LANGSMITH_PROJECT_PUBLIC (PRD §13.2)."""
    (tmp_path / "martian_v1.yaml").write_text("public: true\n")
    monkeypatch.setattr(ls, "_MANIFEST_DIR", tmp_path)
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setenv("LANGSMITH_PROJECT_PUBLIC", "openbot-eval-public")
    monkeypatch.delenv("LANGSMITH_PROJECT_INTERNAL", raising=False)

    with patch.object(ls, "init_client", return_value=MagicMock()) as init_spy:
        ls.init_client_for_dataset("martian_v1")
    init_spy.assert_called_once_with(public=True)


def test_internal_routing_does_not_silently_fall_through_to_public(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hard-to-spot regression: internal-only env shouldn't blow past to public.

    If `is_public_dataset` ever returned True for an internal manifest, the
    init would error on missing LANGSMITH_PROJECT_PUBLIC. This test asserts
    the opposite path: internal dataset never tries to read the public env.
    """
    (tmp_path / "internal_v1.yaml").write_text("public: false\n")
    monkeypatch.setattr(ls, "_MANIFEST_DIR", tmp_path)
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setenv("LANGSMITH_PROJECT_INTERNAL", "openbot-eval-internal")
    monkeypatch.delenv("LANGSMITH_PROJECT_PUBLIC", raising=False)

    # No-op the lazy `from langsmith import Client` so we don't hit network.
    fake_module = type("FakeLS", (), {"Client": lambda **kw: MagicMock()})
    monkeypatch.setitem(__import__("sys").modules, "langsmith", fake_module)

    ls.init_client_for_dataset("internal_v1")  # must NOT raise on missing public env

"""Tests for the config-driven sandbox factory.

The factory dispatches to the docker / modal / daytona backend at runtime
based on ``OPENBOT_SANDBOX_BACKEND`` (or an explicit ``kind=`` arg). We
patch each backend's class methods so the tests stay hermetic — no real
Docker daemon, Modal API call, or Daytona workspace creation.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from evals.common import config as eval_config
from evals.sandboxes import (
    DAYTONA,
    DOCKER,
    MODAL,
    RepoSpec,
    create_bare_sandbox,
    create_sandbox_for_sample,
    get_default_sandbox_kind,
)
from evals.sandboxes import factory as factory_mod


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop any inherited backend env so tests start from a known default."""
    monkeypatch.delenv("OPENBOT_SANDBOX_BACKEND", raising=False)


def test_default_kind_is_daytona() -> None:
    """The factory's project-wide default is Daytona per PRD §3.1."""
    assert get_default_sandbox_kind() == DAYTONA


def test_env_var_selects_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var is case-insensitive and recognises all three backends."""
    monkeypatch.setenv("OPENBOT_SANDBOX_BACKEND", "DOCKER")
    eval_config.get_eval_config.cache_clear()
    assert get_default_sandbox_kind() == DOCKER
    monkeypatch.setenv("OPENBOT_SANDBOX_BACKEND", "modal")
    eval_config.get_eval_config.cache_clear()
    assert get_default_sandbox_kind() == MODAL


def test_unknown_env_var_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo never silently routes the campaign through the wrong runtime.

    Strict pydantic validation now raises at load time instead of falling
    back to the default — an operator sees the bad value immediately
    rather than 50 samples later wondering why daytona ran.
    """
    monkeypatch.setenv("OPENBOT_SANDBOX_BACKEND", "kubernetes")
    eval_config.get_eval_config.cache_clear()
    with pytest.raises(ValidationError):
        get_default_sandbox_kind()


def test_explicit_kind_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing ``kind=`` ignores the env so call sites can force one backend."""

    captured: dict[str, Any] = {}

    async def _docker_factory(**kwargs: Any) -> Any:
        captured["kind"] = "docker"
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setenv("OPENBOT_SANDBOX_BACKEND", "modal")
    monkeypatch.setattr(factory_mod, "create_sandbox_for_sample", create_sandbox_for_sample)
    # Patch each backend's class method so importing them never touches the real SDK.
    from evals.sandboxes.docker_backend import DockerSandboxBackend

    monkeypatch.setattr(DockerSandboxBackend, "create_for_sample", _docker_factory)

    import asyncio

    spec = RepoSpec(repo="x/y", base_commit="abc")
    asyncio.run(create_sandbox_for_sample(repo_spec=spec, kind=DOCKER))

    assert captured["kind"] == "docker"
    assert captured["kwargs"]["repo_spec"] is spec


def test_unsupported_kind_raises() -> None:
    """Unknown ``kind`` arg fails loudly rather than guessing."""
    import asyncio

    with pytest.raises(ValueError, match="Unsupported sandbox backend"):
        asyncio.run(
            create_sandbox_for_sample(
                repo_spec=RepoSpec(repo="x/y", base_commit="abc"),
                kind="kubernetes",
            )
        )


def test_create_bare_dispatches_by_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """``create_bare_sandbox`` follows the same dispatch logic."""
    import asyncio

    captured: dict[str, Any] = {}

    async def _docker_bare(**kwargs: Any) -> Any:
        captured["called"] = "docker"
        captured["kwargs"] = kwargs
        return object()

    from evals.sandboxes.docker_backend import DockerSandboxBackend

    monkeypatch.setattr(DockerSandboxBackend, "create_bare", _docker_bare)

    asyncio.run(create_bare_sandbox(kind=DOCKER, workspace="/tmp/ws"))
    assert captured["called"] == "docker"
    assert captured["kwargs"]["workspace"] == "/tmp/ws"


def test_modal_backend_import_does_not_require_modal_sdk() -> None:
    """Importing the backend module must not crash when ``modal`` is absent.

    The SDK import is deferred to ``create_for_sample`` so the docker /
    daytona codepaths don't get blocked by a missing optional dep.
    """
    from evals.sandboxes import modal_backend

    # Top-level module import must succeed without modal installed; the
    # helper that pulls in the SDK is only called from the factory paths.
    assert hasattr(modal_backend, "ModalSandboxBackend")


def test_daytona_backend_import_does_not_require_daytona_sdk() -> None:
    """Importing the backend module must not crash when daytona-sdk is absent."""
    from evals.sandboxes import daytona_backend

    assert hasattr(daytona_backend, "DaytonaSandboxBackend")

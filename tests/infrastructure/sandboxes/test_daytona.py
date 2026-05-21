"""DaytonaSandboxAdapter — production fix-loop sandbox.

Tests mock the daytona SDK via monkeypatch; no real network. The SDK
surface we exercise is intentionally narrow:
  client.create(params) -> sandbox
  client.delete(sandbox) -> None
  sandbox.process invokes used to run shell commands
  sandbox.files.upload(path, content) -> None
  sandbox.files.download(path) -> bytes
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from openbot.application.ports.sandbox import ExecResult
from openbot.infrastructure.sandboxes.daytona import DaytonaSandboxAdapter


@pytest.fixture
def fake_sandbox() -> Any:
    """A Daytona sandbox-shaped MagicMock with the methods we call."""
    sb = MagicMock()
    sb.id = "sandbox-abc"
    sb.process = MagicMock()
    sb.files = MagicMock()
    # Default returns success
    sb.process.exec.return_value = SimpleNamespace(exit_code=0, result="", additional_properties={})
    return sb


@pytest.fixture
def fake_client(fake_sandbox: Any) -> Any:
    client = MagicMock()
    client.create.return_value = fake_sandbox
    client.delete.return_value = None
    return client


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch, fake_client: Any) -> DaytonaSandboxAdapter:
    """Construct an adapter with the daytona module patched out."""
    fake_daytona_mod = SimpleNamespace(
        Daytona=lambda config=None: fake_client,
        DaytonaConfig=lambda **kwargs: SimpleNamespace(**kwargs),
        CreateSandboxFromImageParams=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(
        "openbot.infrastructure.sandboxes.daytona._get_daytona_module",
        lambda: fake_daytona_mod,
    )
    return DaytonaSandboxAdapter._for_test(client=fake_client)


# ───── create / close lifecycle ─────


async def test_create_provisions_sandbox(monkeypatch: pytest.MonkeyPatch, fake_client: Any) -> None:
    monkeypatch.setattr(
        "openbot.infrastructure.sandboxes.daytona._get_daytona_module",
        lambda: SimpleNamespace(
            Daytona=lambda config=None: fake_client,
            DaytonaConfig=lambda **kw: SimpleNamespace(**kw),
            CreateSandboxFromImageParams=lambda **kw: SimpleNamespace(**kw),
        ),
    )
    monkeypatch.setenv("OPENBOT_DAYTONA_API_KEY", "test-key")
    adapter = await DaytonaSandboxAdapter.create()
    assert fake_client.create.called
    assert adapter.workspace.startswith("/")
    await adapter.close()
    assert fake_client.delete.called


async def test_close_swallows_delete_errors(
    adapter: DaytonaSandboxAdapter, fake_client: Any
) -> None:
    """close() must never raise — sandbox leaks are logged, not propagated."""
    fake_client.delete.side_effect = RuntimeError("daytona transient 500")
    await adapter.close()  # should not raise


# ───── clone ─────


async def test_clone_execs_git_with_x_access_token_url(
    adapter: DaytonaSandboxAdapter, fake_sandbox: Any
) -> None:
    """Token must be injected into the clone URL — never logged separately."""
    await adapter.clone(
        repo_url="https://github.com/YiAgent/openbot.git",
        ref="main",
        token="ghs_xxx",
    )
    # The call list contains the git clone with token-in-URL.
    calls = [str(call.args[0]) for call in fake_sandbox.process.exec.call_args_list]
    assert any("git clone" in c for c in calls)
    assert any("x-access-token:ghs_xxx@github.com/YiAgent/openbot.git" in c for c in calls)


async def test_clone_rejects_non_https_repo_url(adapter: DaytonaSandboxAdapter) -> None:
    """SSH or file URLs would bypass token-in-URL injection — reject loudly."""
    with pytest.raises(ValueError, match="https"):
        await adapter.clone(
            repo_url="git@github.com:YiAgent/openbot.git",
            ref="main",
            token="ghs_xxx",
        )


# ───── run / file IO ─────


async def test_run_normalizes_exec_response_to_exec_result(
    adapter: DaytonaSandboxAdapter, fake_sandbox: Any
) -> None:
    fake_sandbox.process.exec.return_value = SimpleNamespace(
        exit_code=2, result="boom\n", additional_properties={}
    )
    result = await adapter.run(command=["pytest"], timeout_seconds=30)
    assert isinstance(result, ExecResult)
    assert result.exit_code == 2
    # Daytona returns combined stdout+stderr in `result`; we surface it as stdout
    # and leave stderr empty so callers can rely on `(stdout or stderr)` being
    # the human-readable buffer.
    assert result.stdout == "boom\n"
    assert result.stderr == ""
    assert result.timed_out is False


async def test_run_flags_timeout_via_additional_properties(
    adapter: DaytonaSandboxAdapter, fake_sandbox: Any
) -> None:
    """Daytona sets `additional_properties.timed_out` when the call hits the deadline."""
    fake_sandbox.process.exec.return_value = SimpleNamespace(
        exit_code=124, result="", additional_properties={"timed_out": True}
    )
    result = await adapter.run(command=["sleep", "999"], timeout_seconds=1)
    assert result.timed_out is True
    assert result.exit_code == 124

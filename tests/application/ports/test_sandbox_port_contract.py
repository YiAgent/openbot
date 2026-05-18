"""Contract test — FakeSandbox records commands and returns programmed results."""

from __future__ import annotations

import pytest

from tests._fakes.sandbox import FakeSandbox


@pytest.mark.asyncio
async def test_run_returns_default_result() -> None:
    sb = FakeSandbox()
    out = await sb.run(command=["echo", "hi"])
    assert out["exit_code"] == 0
    assert out["timed_out"] is False
    assert sb.calls == [(["echo", "hi"], None, 60)]


@pytest.mark.asyncio
async def test_queued_result_drains_first() -> None:
    sb = FakeSandbox(results=[{"stdout": "x", "stderr": "", "exit_code": 1, "timed_out": False}])
    out = await sb.run(command=["true"])
    assert out["exit_code"] == 1
    # After queue exhausted, falls back to default
    out2 = await sb.run(command=["true"])
    assert out2["exit_code"] == 0


@pytest.mark.asyncio
async def test_env_and_timeout_recorded() -> None:
    sb = FakeSandbox()
    await sb.run(command=["ls"], env={"HOME": "/tmp"}, timeout_seconds=30)
    assert sb.calls[0] == (["ls"], {"HOME": "/tmp"}, 30)

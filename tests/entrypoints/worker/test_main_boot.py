"""Boot smoke — worker entrypoint loads and surfaces missing-config error fast.

The worker's ``_main()`` checks for ``OPENBOT_REDIS_URL`` at boot and returns 2
immediately when it is unset — no connection attempt, no hang.
"""

from __future__ import annotations

import importlib

import pytest


def test_worker_module_importable() -> None:
    """Module imports cleanly with no side effects."""
    mod = importlib.import_module("openbot.entrypoints.worker.__main__")
    assert hasattr(mod, "main")


@pytest.mark.asyncio
async def test_worker_main_exits_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """With OPENBOT_REDIS_URL unset, _main() returns non-zero exit code quickly.

    The worker checks for redis_url at boot and returns 2 without attempting
    any network connections.  We call _main() directly (bypassing asyncio.run)
    so that pytest-asyncio owns the event loop and no unclosed-loop warnings
    leak into subsequent tests.
    """
    from openbot.core.settings import get_settings

    monkeypatch.delenv("OPENBOT_REDIS_URL", raising=False)
    get_settings.cache_clear()

    try:
        mod = importlib.import_module("openbot.entrypoints.worker.__main__")
        assert hasattr(mod, "_main"), "worker __main__ must expose _main() coroutine"
        result = await mod._main()
        assert result != 0, "worker _main() should return non-zero when Redis is not configured"
    finally:
        get_settings.cache_clear()

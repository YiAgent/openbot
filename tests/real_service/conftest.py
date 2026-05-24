"""Real-service layer conftest.

Provides ``_env_or_skip`` — a module-level skip helper for tests that
require real external connections. Call it at the top of each real-
service test module, not per-test, so missing env produces one ``s``
per file rather than one per test.

Also exports cassette path constants so individual test files don't
hardcode paths.

Forbidden: business assembly, use-case wiring, agent runtime.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# ── Cassette paths ────────────────────────────────────────────────────────────

CASSETTES_DIR: Path = Path(__file__).parent / "github" / "cassettes"
SMEE_FIXTURES_DIR: Path = Path(__file__).parent / "smee" / "fixtures" / "recorded_deliveries"


# ── Module-level skip helper ──────────────────────────────────────────────────


def _env_or_skip(*keys: str) -> dict[str, str]:
    """Return env values for *keys* or skip the entire module if any are missing.

    Usage (at module top-level, before any test function)::

        from tests.real_service.conftest import _env_or_skip

        env = _env_or_skip("OPENBOT_DATABASE_URL")

    Per spec §10.1: module-level skip produces one ``s`` per file,
    not one per test, which keeps real-service output readable.
    """
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        pytest.skip(
            f"missing env: {', '.join(missing)}",
            allow_module_level=True,
        )
    return {k: os.environ[k] for k in keys}

"""Centralized logger configuration shared by api + worker + cli."""

from __future__ import annotations

import logging
import os


def configure_root_logger() -> None:
    """Idempotent — safe to call from any entrypoint."""
    level = os.environ.get("OPENBOT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=False,
    )

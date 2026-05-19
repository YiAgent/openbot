"""Database schema bootstrap CLI."""

from __future__ import annotations

import asyncio
import logging
import sys

from openbot.core.settings import get_settings
from openbot.infrastructure.persistence import create_schema, make_engine

_logger = logging.getLogger(__name__)


async def _main(argv: list[str]) -> int:
    del argv
    settings = get_settings()
    if not settings.postgres_url:
        _logger.error("db_init_missing_postgres_url")
        return 2

    engine = make_engine(settings.postgres_url, echo=settings.debug)
    try:
        await create_schema(engine)
    finally:
        await engine.dispose()
    _logger.info("db_init_complete")
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())

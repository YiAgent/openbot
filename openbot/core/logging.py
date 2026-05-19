"""Centralized logger configuration shared by api + worker + cli.

Emits **JSON lines** so Papertrail / log aggregators can filter on
structured fields rather than parsing text.  Every existing call to

    _logger.info("event_name", extra={"key": "value", ...})

produces a single JSON object on stdout, e.g.::

    {
        "timestamp": "2026-05-19T14:00:00.123Z",
        "level": "INFO",
        "logger": "openbot.entrypoints.api.app",
        "message": "http_request_completed",
        "method": "POST",
        "path": "/webhook/github",
        "status_code": 202,
        "request_id": "abc123",
        "elapsed_ms": 12.3,
    }

Fields from ``extra`` are merged into the top-level object — no nesting,
no ``extra`` wrapper key — making Papertrail field search work directly.

Falls back to plain-text ``basicConfig`` if ``python-json-logger`` is not
installed (a slim image without the dep still boots cleanly; a WARNING
tells the operator to install it).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime


def _build_json_formatter() -> logging.Formatter:
    """Return a JSON formatter from python-json-logger.

    Import is deferred so that import-time failures (missing dep) are
    caught by configure_root_logger and handled with a fallback.
    """
    from pythonjsonlogger import jsonlogger  # type: ignore[import-untyped]

    class _OpenBotJsonFormatter(jsonlogger.JsonFormatter):
        """Opinionated JSON format for OpenBot log lines.

        Key decisions:
        - ``timestamp`` is always UTC ISO-8601 (not the locale-aware
          ``asctime`` format that basicConfig produces).
        - ``level`` / ``logger`` / ``message`` come before extra fields
          so the start of each JSON line is consistent.
        - ``exc_info`` blocks are serialised to ``exception`` so
          aggregators can index them without special-casing.
        """

        def add_fields(
            self,
            log_record: dict,
            record: logging.LogRecord,
            message_dict: dict,
        ) -> None:
            super().add_fields(log_record, record, message_dict)
            # Always emit a stable UTC timestamp — overrides whatever
            # JsonFormatter computed from asctime.
            log_record["timestamp"] = datetime.fromtimestamp(record.created, tz=UTC).isoformat()
            log_record["level"] = record.levelname
            log_record["logger"] = record.name
            # Remove the raw levelname/name that JsonFormatter duplicates.
            log_record.pop("levelname", None)
            log_record.pop("name", None)

    return _OpenBotJsonFormatter("%(message)s %(levelname)s %(name)s")


def configure_root_logger() -> None:
    """Idempotent — safe to call from any entrypoint.

    Configures the root logger with a JSON formatter. If python-json-logger
    is not available, falls back to a plain-text basicConfig and emits a
    WARNING so the gap is visible in the logs.

    ``force=True`` is used when installing the JSON handler so that Uvicorn
    (which configures the root logger before our lifespan runs) and pytest
    (which sets up its own capturing handler) are replaced. Without ``force``
    the JSON formatter would silently be ignored whenever another handler
    got there first.
    """
    level_name = os.environ.get("OPENBOT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    try:
        json_formatter = _build_json_formatter()
    except ImportError:
        # Slim install without python-json-logger. Fall back gracefully.
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            force=False,
        )
        logging.getLogger(__name__).warning("python_json_logger_not_installed_falling_back_to_text")
        return

    handler = logging.StreamHandler()
    handler.setFormatter(json_formatter)
    logging.basicConfig(handlers=[handler], level=level, force=True)

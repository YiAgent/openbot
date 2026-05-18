"""Phase-1 shim — re-export the FastAPI app from its new home."""

from openbot.entrypoints.api.app import app  # noqa: F401

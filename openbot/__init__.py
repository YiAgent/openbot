"""OpenBot — open-source, self-hosted GitHub maintenance bot.

See `docs/prd/openbot-prd.md` for the product spec.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("openbot")
except PackageNotFoundError:  # editable install pre-`uv sync`
    __version__ = "0.0.0+local"

__all__ = ["__version__"]

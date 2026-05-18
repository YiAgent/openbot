"""Phase-1 shim — re-export the new core.settings module."""

from openbot.core.settings import *  # noqa: F403
from openbot.core.settings import Settings, get_settings  # noqa: F401

"""Phase-1 shim — re-export the new domain module."""

from openbot.domain.events import *  # noqa: F403
from openbot.domain.events import EventKind, UnifiedEvent  # noqa: F401

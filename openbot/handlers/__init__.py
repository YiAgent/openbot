"""Phase-1 shim — re-export from application.handlers."""

import sys as _sys

from openbot.application.handlers import *  # noqa: F403
from openbot.application.handlers import (
    debug_echo,
    debug_echo_handler,  # noqa: F401
)

_sys.modules[__name__ + ".debug_echo"] = debug_echo

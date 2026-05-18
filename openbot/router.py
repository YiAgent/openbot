"""Phase-1 shim — re-export from application.router."""

import sys as _sys

from openbot.application import router as _router_mod  # noqa: F401
from openbot.application.router import *  # noqa: F403
from openbot.application.router import (  # noqa: F401
    _CHAT_PREFIX_DEFAULT,
    Dispatch,
    derive_run_id,
    derive_task_id,
    dispatch_for,
    upgrade_dispatch,
)

# Belt-and-suspenders: register in sys.modules so callers that do
# `import openbot.router` and then access attributes get this module.
_sys.modules.setdefault(__name__, _sys.modules[__name__])

"""Phase-1 shim — re-export from application.dispatcher.

``load_for_repo`` and ``run_preflight`` are exposed here so existing
monkeypatch targets of the form
``monkeypatch.setattr("openbot.dispatch.X", ...)`` continue to work.
``run_dispatch`` in ``application.dispatcher`` resolves both via
``sys.modules["openbot.dispatch"]`` at call time so it sees patched values.
"""

import sys as _sys

from openbot.application.dispatcher import *  # noqa: F403
from openbot.application.dispatcher import build_preflight_chain, run_dispatch  # noqa: F401
from openbot.application.middleware import run_preflight  # noqa: F401  # patchable by tests
from openbot.config_repo import load_for_repo  # noqa: F401  # patchable by tests

# Belt-and-suspenders: ensure this module is registered so the sys.modules
# lookup inside run_dispatch finds it.
_sys.modules.setdefault(__name__, _sys.modules[__name__])

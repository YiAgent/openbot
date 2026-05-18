"""Phase-1 shim — re-export from application.workflows."""

import sys as _sys

from openbot.application.workflows import *  # noqa: F403
from openbot.application.workflows import (  # noqa: F401  # noqa: F401
    _lifecycle,
    chat,
    chat_parser,
    fix,
    maybe_run_chat,
    maybe_run_fix,
    maybe_run_review,
    maybe_run_triage,
    review,
    triage,
)

_sys.modules[__name__ + "._lifecycle"] = _lifecycle
_sys.modules[__name__ + ".chat"] = chat
_sys.modules[__name__ + ".chat_parser"] = chat_parser
_sys.modules[__name__ + ".fix"] = fix
_sys.modules[__name__ + ".review"] = review
_sys.modules[__name__ + ".triage"] = triage

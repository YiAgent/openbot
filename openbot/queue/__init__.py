"""Phase-1 shim — re-export from infrastructure.queue."""

import importlib as _importlib
import sys as _sys

from openbot.infrastructure.queue import *  # noqa: F403
from openbot.infrastructure.queue import (
    enqueue as enqueue,
)
from openbot.infrastructure.queue import (
    payload as payload,
)
from openbot.infrastructure.queue import (
    runner as runner,
)
from openbot.infrastructure.queue import (
    worker as worker,
)

# `from X import Y` already binds Y as an attribute of this module, which
# is what `monkeypatch.setattr` needs (it walks via getattr, not importlib).
# The sys.modules registration is belt-and-suspenders for direct module imports.
#
# NOTE: enqueue is a function (not a module), so we must import the actual
# module separately for sys.modules registration.
_enqueue_mod = _importlib.import_module("openbot.infrastructure.queue.enqueue")
_sys.modules[__name__ + ".enqueue"] = _enqueue_mod
_sys.modules[__name__ + ".payload"] = payload
_sys.modules[__name__ + ".runner"] = runner
_sys.modules[__name__ + ".worker"] = worker

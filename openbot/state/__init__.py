"""Phase-1 shim — re-export from application.state + domain.intents.

``runs_repo`` is a separate file shim (openbot/state/runs_repo.py) to avoid a
circular import between openbot.persistence.models and this __init__.py.
"""

import sys as _sys

from openbot.application.state import (
    cancellation,
    classifier,
    resource_lock,
)
from openbot.domain import intents
from openbot.domain.intents import EventClassification, Intent, State  # noqa: F401

# Register submodules so `import openbot.state.X` / `from openbot.state.X import Y` work.
# runs_repo resolves via the file shim openbot/state/runs_repo.py on disk.
_sys.modules[__name__ + ".cancellation"] = cancellation
_sys.modules[__name__ + ".classifier"] = classifier
_sys.modules[__name__ + ".resource_lock"] = resource_lock
_sys.modules[__name__ + ".intents"] = intents

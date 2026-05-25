"""Compat shim — re-exports from openbot.testing.fakes for legacy flat test files.

PR #87 moved test fakes from ``tests/_fakes/`` to ``openbot/testing/fakes/``
(port-fake pattern). Flat test files from main that haven't been restructured
yet import through this shim so their imports continue to work.
"""

# Re-export each submodule.
from openbot.testing.fakes import (
    channel_adapter,  # noqa: F401
    config_loader,  # noqa: F401
    sandbox,  # noqa: F401
)

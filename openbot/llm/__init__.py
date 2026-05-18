"""Phase-1 shim — re-export from infrastructure.llm.

`openbot.llm.router` was renamed to `model_router` in infrastructure
to avoid colliding with `openbot.application.router`. The shim re-aliases
it so legacy `from openbot.llm.router import Feature` keeps resolving
until Task 1.11 rewrites callers.

``complete`` and ``sanitize`` are NOT imported eagerly here.  Their
sys.modules aliases are set lazily on first access via ``__getattr__``
to avoid a circular import chain:

  openbot.persistence.models → openbot.llm.router (this shim)
    → complete.py → openbot.persistence.models  (circular!)

By deferring ``complete`` and ``sanitize`` until something explicitly
asks for them, the persistence bootstrap finishes first.
"""

import sys as _sys
from types import ModuleType as _ModuleType

from openbot.infrastructure.llm import *  # noqa: F403
from openbot.infrastructure.llm import model_router
from openbot.infrastructure.llm import model_router as router

_sys.modules[__name__ + ".router"] = router
_sys.modules[__name__ + ".model_router"] = model_router


def __getattr__(name: str) -> _ModuleType:
    """Lazily resolve ``complete`` and ``sanitize`` submodule aliases.

    Each branch uses a literal import path so static scanners can verify
    that no untrusted input reaches ``import_module``.
    """
    import importlib as _importlib

    if name == "complete":
        mod = _importlib.import_module("openbot.infrastructure.llm.complete")
        _sys.modules[__name__ + ".complete"] = mod
        return mod
    if name == "sanitize":
        mod = _importlib.import_module("openbot.infrastructure.llm.sanitize")
        _sys.modules[__name__ + ".sanitize"] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

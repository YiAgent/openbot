"""Phase-1 shim — re-export from infrastructure.adapters."""

import sys as _sys

from openbot.infrastructure.adapters import *  # noqa: F403
from openbot.infrastructure.adapters import (
    base as base,
)
from openbot.infrastructure.adapters import (
    github as github,
)
from openbot.infrastructure.adapters import (
    github_auth as github_auth,
)

# `from X import Y` binds Y as an attribute of this module, which is what
# `getattr` walks (e.g. monkeypatch.setattr) need.  The sys.modules
# registration is belt-and-suspenders for direct `import` use.
_sys.modules[__name__ + ".base"] = base
_sys.modules[__name__ + ".github"] = github
_sys.modules[__name__ + ".github_auth"] = github_auth

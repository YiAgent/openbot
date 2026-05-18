"""Phase-1 shim — re-export from infrastructure.persistence."""

import sys as _sys

from openbot.infrastructure.persistence import *  # noqa: F403
from openbot.infrastructure.persistence import (
    db as db,
)
from openbot.infrastructure.persistence import (
    dedup as dedup,
)
from openbot.infrastructure.persistence import (
    models as models,
)
from openbot.infrastructure.persistence import (
    redis as redis,
)
from openbot.infrastructure.persistence import (
    repository as repository,
)

# `from X import Y` binds Y as an attribute of this module, which is what
# `getattr` walks (e.g. monkeypatch.setattr) need.  The sys.modules
# registration is belt-and-suspenders for direct `import` use.
_sys.modules[__name__ + ".db"] = db
_sys.modules[__name__ + ".dedup"] = dedup
_sys.modules[__name__ + ".models"] = models
_sys.modules[__name__ + ".redis"] = redis
_sys.modules[__name__ + ".repository"] = repository

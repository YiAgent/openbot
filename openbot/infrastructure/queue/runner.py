"""Phase-1 shim — kept until Procfile flips in Phase 4.

The real runner module is now at openbot/entrypoints/worker/__main__.py.
This shim delegates execution to the new location.

The Task 1.4 shim at `openbot/queue/__init__.py` re-exports this
`runner` submodule so `import openbot.queue.runner` continues to work.
Procfile updates happen in Phase 4 (Task 4.1).
"""

from __future__ import annotations

import sys

from openbot.entrypoints.worker.__main__ import main

if __name__ == "__main__":
    sys.exit(main())

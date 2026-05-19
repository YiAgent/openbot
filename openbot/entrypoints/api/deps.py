"""FastAPI dependency factories — Phase 1 placeholder.

In Phase 2 this module will expose `get_dedup`, `get_queue`, `get_runs_repo`,
etc. Each factory returns an infrastructure implementation typed as the
application-layer Port. Until then, route handlers read collaborators
directly from `request.app.state.*`.
"""

from __future__ import annotations

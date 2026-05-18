"""Application-layer Port catalogue.

Each Port is a `typing.Protocol` defined in its own module. Infrastructure
adapters satisfy these structurally — they import the Protocol only under
`TYPE_CHECKING` so the runtime arrow stays infra → domain, never
infra → application.

Each subsequent Port task appends one re-export here.
"""

from __future__ import annotations

from openbot.application.ports.dedup import DedupPort  # noqa: F401

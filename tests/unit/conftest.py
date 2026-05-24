"""Unit-layer conftest. Intentionally near-empty.

Per spec §8.2: unit tests forbid IO fixtures. Add only pure helpers
here, never anything that opens a connection / spawns a process.
"""

from __future__ import annotations

"""Fake implementations of openbot.application.ports.* protocols.

Each fake mirrors its port Protocol exactly (verified by a module-level
_PROTOCOL_CHECK assignment). Observable state is exposed as immutable
tuples of frozen dataclasses; no `.calls: list[dict]` weak typing.
Failure injection is explicit (constructor kwargs), never via env vars.
"""

from __future__ import annotations

__all__: list[str] = []

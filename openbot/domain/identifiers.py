"""Pure derivations — task_id and run_id. No I/O."""

from __future__ import annotations

import hashlib

from openbot.domain.events import UnifiedEvent


def derive_task_id(event: UnifiedEvent) -> str:
    """Stable 128-bit hex task_id (harness spec §9.1)."""
    material = f"{event.channel}|{event.repo}|{event.delivery_id}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


def derive_run_id(resource_key: str, serial: int) -> str:
    """Per-resource run identifier — stable across (resource, serial).

    ``resource_key`` keeps the id stably scoped to one issue/PR so an
    operator can grep ``logs | grep <prefix>`` to follow a single
    conversation. ``serial`` is a caller-supplied tiebreaker (typically
    ``time.monotonic_ns()``) so two simultaneous START intents for the
    same resource end up with distinct ids. 32 hex chars matches the
    ``cost_meter.task_id`` column width so handlers can stuff either
    value in without overflowing.
    """
    material = f"{resource_key}|{serial}".encode()
    return hashlib.sha256(material).hexdigest()[:32]

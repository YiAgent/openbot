"""DedupOutcome — domain-level result type for webhook delivery deduplication.

PRD §5.1: dedupe by ``X-GitHub-Delivery`` so GitHub's retry (slow handler,
non-2xx response, network blip) doesn't replay a workflow twice.

The three outcomes (``FRESH``, ``DUPLICATE``, ``FALLBACK_OPEN``) are an enum
rather than a ``(bool, bool)`` pair so call sites cannot accidentally drop
the ``FALLBACK_OPEN`` audit signal via a truthy shortcut.
"""

from __future__ import annotations

from enum import StrEnum


class DedupOutcome(StrEnum):
    """The three possible results of ``check_and_mark``.

    Encoded as an enum (not a two-bool pair) so call sites cannot accidentally
    collapse the ``FALLBACK_OPEN`` audit signal into truthiness.

    Usage:
        outcome = await dedup.check_and_mark(channel, delivery_id)
        if outcome is DedupOutcome.DUPLICATE:
            return  # drop
        # otherwise FRESH or FALLBACK_OPEN — process, but audit-log the latter
    """

    FRESH = "fresh"
    """First time we've seen this delivery_id — proceed."""

    DUPLICATE = "duplicate"
    """A previous webhook already marked the key — drop, do not re-run workflow."""

    FALLBACK_OPEN = "fallback_open"
    """Redis was unreachable or unconfigured; we couldn't dedup. The caller
    SHOULD process the event (a brief dedup outage is preferable to dropping
    all webhooks), AND SHOULD audit-log this so silent dedup degradation
    is observable."""

    @property
    def should_process(self) -> bool:
        """True iff the workflow should run.

        ``FRESH`` and ``FALLBACK_OPEN`` both proceed; only ``DUPLICATE`` short-circuits.
        Use this when you want the boolean question and don't care which of the
        two "yes" cases applies.
        """
        return self is not DedupOutcome.DUPLICATE

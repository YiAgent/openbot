"""Queue payload schema — JSON-serializable shape of one enqueued workflow.

The webhook handler builds a ``QueuePayload`` from the UnifiedEvent +
Dispatch and ``enqueue()`` writes it to ``openbot:workflows``. The worker
reads + deserializes back into the same dataclass and then reconstructs
the in-memory ``UnifiedEvent`` / ``Dispatch`` for dispatch.

JSON-only is a deliberate choice over Python-native binary serialization:

  - **Forward/backward compat**: a v0.2 worker reading a v0.1-enqueued
    entry sees a stable JSON shape; binary serialization would tie the
    queue to the Python class layout.
  - **Inspection**: ``XRANGE openbot:workflows`` from redis-cli yields
    human-readable JSON, not a binary blob, when debugging a stuck
    queue.
  - **Cross-version safety**: binary serialization formats can run
    arbitrary code on load; ``json.loads`` cannot.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from openbot.events import EventKind, UnifiedEvent
from openbot.llm.router import Feature

_logger = logging.getLogger(__name__)

# Schema version for the JSON payload. Bump when fields are added/removed
# so old workers reject (or upgrade) entries they can't parse. v0.1: 1.
PAYLOAD_VERSION: Final = 1

STREAM_NAME: Final = "openbot:workflows"
GROUP_NAME: Final = "openbot:workflows:group"
DEAD_STREAM: Final = "openbot:workflows:dead"

# Bounded stream — Redis Streams without MAXLEN grow unbounded and a
# stuck worker pool will OOM Redis. 10k entries at ~2 KB each gives
# a ~20 MB ceiling. At PRD §11 v0.1 alpha targets (~10 events/day per
# user) this is months of headroom.
MAX_STREAM_LEN: Final = 10_000


@dataclass(frozen=True, slots=True)
class QueuePayload:
    """One enqueued workflow's worth of data.

    Mirrors the UnifiedEvent fields the workflow handler needs, plus the
    dispatch decision (feature + task_id) already made by the Router.
    """

    version: int
    channel: str
    delivery_id: str
    kind: str  # EventKind.value (stringly-typed for JSON round-trip)
    repo: str
    actor: str
    actor_type: str | None
    issue_number: int | None
    pr_number: int | None
    comment_body: str | None
    installation_id: int | None
    raw: dict[str, Any]
    feature: str  # Feature.value
    task_id: str
    enqueued_at: str  # ISO 8601, UTC

    @classmethod
    def from_event(
        cls,
        event: UnifiedEvent,
        *,
        feature: Feature,
        task_id: str,
    ) -> QueuePayload:
        return cls(
            version=PAYLOAD_VERSION,
            channel=event.channel,
            delivery_id=event.delivery_id,
            kind=event.kind.value,
            repo=event.repo,
            actor=event.actor,
            actor_type=event.actor_type,
            issue_number=event.issue_number,
            pr_number=event.pr_number,
            comment_body=event.comment_body,
            installation_id=event.installation_id,
            raw=event.raw,
            feature=feature.value,
            task_id=task_id,
            enqueued_at=datetime.now(UTC).isoformat(),
        )

    def to_event(self) -> UnifiedEvent:
        """Reverse of ``from_event``. Unknown EventKind values fall back
        to UNKNOWN rather than raising — the worker should still drain
        the entry and let the workflow stub log a structured skip."""
        try:
            kind = EventKind(self.kind)
        except ValueError:
            _logger.warning(
                "queue_payload_unknown_kind",
                extra={"delivery_id": self.delivery_id, "kind_raw": self.kind},
            )
            kind = EventKind.UNKNOWN
        return UnifiedEvent(
            channel=self.channel,
            delivery_id=self.delivery_id,
            kind=kind,
            repo=self.repo,
            actor=self.actor,
            actor_type=self.actor_type,
            issue_number=self.issue_number,
            pr_number=self.pr_number,
            comment_body=self.comment_body,
            installation_id=self.installation_id,
            raw=self.raw,
        )

    def to_json(self) -> str:
        # `asdict` is intentional: the dataclass shape IS the JSON shape,
        # so any future field appears in both serialize and deserialize
        # automatically.
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


def deserialize_payload(blob: str | bytes) -> QueuePayload | None:
    """Parse a queue entry. Returns None on malformed / version mismatch.

    Returning None (not raising) lets the worker decide whether to DLQ
    or skip. A raise would tank the consumer loop on the first bad row.
    """
    try:
        text = blob.decode("utf-8") if isinstance(blob, bytes | bytearray) else blob
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _logger.warning(
            "queue_payload_parse_failed",
            extra={"reason": f"{type(exc).__name__}: {exc}"},
        )
        return None
    if not isinstance(data, dict):
        _logger.warning("queue_payload_not_object", extra={"got": type(data).__name__})
        return None
    version = data.get("version")
    if version != PAYLOAD_VERSION:
        _logger.warning(
            "queue_payload_version_mismatch",
            extra={"got": version, "expected": PAYLOAD_VERSION},
        )
        return None
    try:
        return QueuePayload(**data)
    except TypeError as exc:
        # Missing / extra field — schema drift, log + skip.
        _logger.warning(
            "queue_payload_schema_drift",
            extra={"reason": str(exc)[:200], "delivery_id": data.get("delivery_id")},
        )
        return None


# Suppress unused-name warning on `field`; kept as a clean import path
# so a future field with `field(default_factory=...)` doesn't need a
# second edit here.
_ = field

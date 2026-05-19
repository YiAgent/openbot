"""Queue payload schema — JSON-serializable shape of one enqueued workflow.

The webhook handler builds a ``QueuePayload`` from the UnifiedEvent +
Dispatch and ``enqueue()`` writes it to the ``openbot:workflows`` Redis stream. The worker
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

Schema versions:

  v1 — original. Carries ``task_id`` only (the per-delivery id from
       ``router.derive_task_id``).
  v2 — adds state-machine fields:
       ``intent``        START / SUPERSEDE / CANCEL / IGNORE (the
                         classifier's decision; the worker uses this
                         to gate ``cancellation.signal`` etc.).
       ``run_id``        per-resource run id from ``router.derive_run_id``.
                         Replaces ``task_id`` semantically — but
                         ``task_id`` stays in the payload as an alias
                         (== ``run_id``) so ``cost_meter`` rows don't
                         need a schema change this slice.
       ``prev_run_id``   the run id we're superseding/cancelling. Empty
                         when ``intent`` is START or IGNORE.
       ``resource_key``  ``github:owner/repo:pr:42`` etc. Lets the
                         worker reconstruct the state-machine cache
                         key without re-parsing.
       ``event_seq``     epoch-ms ``updated_at`` of the resource at
                         classify time — preserved for audit logs.

Backward compat: ``deserialize_payload`` accepts both v1 and v2. v1
entries get ``intent=START``, ``run_id=task_id``, and the other v2
fields default to None — letting a worker rolling-upgrade past an
in-flight v1 batch without dropping entries.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.llm.model_router import Feature

_logger = logging.getLogger(__name__)

# Schema version for the JSON payload. Bump when fields are added/removed
# so old workers reject (or upgrade) entries they can't parse. v0.1: 1;
# state-machine slice: 2.
PAYLOAD_VERSION: Final = 2

# The set of versions we know how to deserialize. The producer always
# emits ``PAYLOAD_VERSION``; the consumer is allowed to consume any
# version in this set so a worker rolling-upgrade doesn't drop entries
# emitted by the older receive side.
_SUPPORTED_VERSIONS: Final = frozenset({1, 2})

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
    dispatch decision (feature + run_id) already made by the receive
    side. New v2 fields default-None so v1 entries (no intent, no
    resource_key) deserialize cleanly.
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
    # GitHub check_run_id — when present, the worker updates the status
    # to 'completed' after the workflow finishes.
    check_run_id: int | None = None
    # ── v2 state-machine fields ──
    intent: str | None = None  # Intent.value
    run_id: str | None = None  # alias of task_id; explicit for clarity
    prev_run_id: str | None = None
    resource_key: str | None = None
    event_seq: int = 0

    @classmethod
    def from_event(
        cls,
        event: UnifiedEvent,
        *,
        feature: Feature,
        task_id: str,
        check_run_id: int | None = None,
        intent: str | None = None,
        run_id: str | None = None,
        prev_run_id: str | None = None,
        resource_key: str | None = None,
        event_seq: int | None = None,
    ) -> QueuePayload:
        """Build a payload from a UnifiedEvent + Dispatch.

        Receive-side callers either pass the full v2 quintuple
        (``intent`` / ``run_id`` / ``prev_run_id`` / ``resource_key`` /
        ``event_seq``) when the state machine has classified the event,
        or leave them None for v1-compat callers. The state-machine
        path always passes them; legacy unit tests stay green by
        omitting them.
        """
        # event_seq comes from the classified event; default to whatever
        # is on the UnifiedEvent (the GitHub adapter already populates
        # this from ``updated_at``).
        seq = event_seq if event_seq is not None else event.event_seq
        # run_id defaults to task_id — they're the same value in v0.1.
        rid = run_id if run_id is not None else task_id
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
            check_run_id=check_run_id,
            intent=intent,
            run_id=rid,
            prev_run_id=prev_run_id,
            resource_key=resource_key,
            event_seq=seq,
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
            event_seq=self.event_seq,
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

    Backward compat: v1 entries (no ``intent`` / ``run_id`` / etc.) are
    upgraded in-place to v2-shaped dataclass instances — ``intent``
    defaults to ``"start"`` (the only behaviour v1 ever produced) and
    ``run_id`` mirrors ``task_id`` so handlers can use the new field
    uniformly. This lets a worker rolling-upgrade past in-flight v1
    entries without DLQ'ing them.
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
    if version not in _SUPPORTED_VERSIONS:
        _logger.warning(
            "queue_payload_version_unsupported",
            extra={"got": version, "supported": sorted(_SUPPORTED_VERSIONS)},
        )
        return None

    # v1 → v2 forward compat: synthesize the new fields with safe
    # defaults so the dataclass constructor doesn't TypeError. We always
    # restamp ``version`` to the current payload version so downstream
    # code can rely on a single shape.
    if version == 1:
        data.setdefault("intent", "start")
        data.setdefault("run_id", data.get("task_id"))
        data.setdefault("prev_run_id", None)
        data.setdefault("resource_key", None)
        data.setdefault("event_seq", 0)
        data["version"] = PAYLOAD_VERSION

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

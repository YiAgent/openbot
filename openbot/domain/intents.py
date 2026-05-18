"""Intent + State enums + EventClassification dataclass.

These are the pure value types the classifier returns and the runs repo
persists. No I/O, no dependencies beyond ``openbot.domain.events`` and the
stdlib so the test for the classifier is a table-driven pure-function
exercise.

``State`` is the per-resource conversation state stored in
``task_runs.state``. ``Intent`` is the per-delivery classification that
drives the worker's behaviour. The two are deliberately separate types:
multiple intents (e.g. ``START`` after ``CLOSED``, ``SUPERSEDE`` while
``RUNNING``) can leave the resource in the same state (``RUNNING``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    """Per-resource conversation state, persisted in ``task_runs.state``.

    ``IDLE``       no run is active. New triage/review/fix may start.
    ``RUNNING``    a worker is (or recently was) handling this resource.
                   A new event for the same resource will SUPERSEDE or CANCEL.
    ``CANCELLED``  the previous run was cancelled (close, user @cancel, etc.).
                   Functionally equivalent to ``IDLE`` for future events;
                   we keep it distinct so audit-log queries can see the
                   "was running, then cancelled" trail.
    ``CLOSED``     issue/PR is closed/merged. ``REOPENED`` events transition
                   back to ``IDLE`` and a new run may start.
    """

    IDLE = "idle"
    RUNNING = "running"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class Intent(StrEnum):
    """One webhook delivery's classification.

    ``START``      begin a new run for this resource. Allocates a fresh ``run_id``.
    ``SUPERSEDE``  cancel the in-flight run (if any) and start a new one.
                   Used for ``pr.synchronize`` (new commit), ``@openbot``
                   follow-up comments, and re-assignments to the bot.
    ``CANCEL``     stop the in-flight run; do not start a new one.
                   Used for ``issue.closed``, ``pr.closed``, ``pr.merged``.
    ``IGNORE``     drop the event silently. Used for bot-authored events,
                   missing ``@openbot`` mentions, ``ping``, and any event
                   whose ``event_seq`` is older than the persisted high-water
                   mark (out-of-order arrival).
    """

    START = "start"
    SUPERSEDE = "supersede"
    CANCEL = "cancel"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class EventClassification:
    """The classifier's return: what the receive-side should do next.

    ``intent``         which of the four actions to take.
    ``next_state``     the ``State`` value to persist in ``task_runs.state``.
                       For ``IGNORE`` the caller should NOT write — kept
                       on the dataclass purely so the caller has one
                       shape to inspect.
    ``reason``         short stable identifier for logs / audit details
                       (e.g. ``"stale_event"``, ``"bot_actor"``). Never
                       includes user content.
    """

    intent: Intent
    next_state: State
    reason: str | None = None

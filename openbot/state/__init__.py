"""Per-resource state machine — webhook → intent → persisted transition.

Each GitHub webhook for an issue or PR is classified into one of four
canonical intents (``START`` / ``SUPERSEDE`` / ``CANCEL`` / ``IGNORE``)
based on the event kind and the resource's current state. The classified
intent + a CAS-guarded ``TaskRun`` write drives the supersede / cancel /
ignore-stale semantics the upstream queue cannot express on its own
(see ``docs/_archive/slices/2026-05-17-input-side-completeness.md``).

Modules:

  - ``intents``        Intent / State enums + EventClassification dataclass.
  - ``classifier``     pure ``classify(event, current_state) -> EventClassification``.
  - ``runs_repo``      SQLAlchemy CRUD for ``task_runs`` (CAS via ``version_id_col``).
  - ``resource_lock``  thin Redis SET NX EX wrapper with bounded retry.
  - ``cancellation``   in-process asyncio.Task registry + Redis cancel flag.
"""

from openbot.state.intents import EventClassification, Intent, State

__all__ = [
    "EventClassification",
    "Intent",
    "State",
]

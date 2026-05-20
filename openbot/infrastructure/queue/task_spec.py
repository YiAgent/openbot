# openbot/infrastructure/queue/task_spec.py
"""TaskSpec v3 — self-contained worker input contract.

Discriminated from QueuePayload (which uses "version": 1|2) by "spec_version": 3.
Built by the webhook async segment after running full preflight. The worker
trusts this and executes the handler without re-running any preflight gates.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from openbot.domain.events import EventKind, UnifiedEvent

if TYPE_CHECKING:
    from openbot.application.router import Dispatch

_logger = logging.getLogger(__name__)
TASK_SPEC_VERSION: Final = 3

# Note: the webhook decision trace is serialized as a `list[dict[str, Any]]`
# (see `decision_trace` field below). A typed `DecisionStep` dataclass was
# considered during F1 design but never wired — the trace is built directly
# as dicts in openbot/dispatcher/decide.py to keep JSON serialization trivial.


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """TaskSpec v3 — worker input contract built by webhook async segment."""

    spec_version: int  # always 3
    # identity
    task_id: str
    run_id: str
    prev_run_id: str | None
    resource_key: str | None
    event_seq: int
    intent: str  # "start" | "supersede" | "cancel"
    enqueued_at: str  # ISO-8601 UTC
    spec_built_at: str  # ISO-8601 UTC (when webhook built this)
    # scenario
    scenario: str  # Feature.value
    # event reconstruction
    channel: str
    delivery_id: str
    kind: str  # EventKind.value
    repo: str
    actor: str
    actor_type: str | None
    issue_number: int | None
    pr_number: int | None
    comment_body: str | None
    installation_id: int | None
    raw: dict[str, Any]
    check_run_id: int | None
    # decision proof
    decision_trace: list[dict[str, Any]]
    classifier_skipped: bool  # True when classifier_output is None
    stages_to_run: list[str]  # [] = run all stages
    # cancel quick-check
    initial_labels: list[str]  # label snapshot at spec-build time
    # Optional with defaults — backward-compat with older v3 JSON payloads.
    classifier_output: dict[str, Any] | None = None
    is_incremental: bool = False
    is_force_push: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    def to_event(self) -> UnifiedEvent:
        try:
            kind = EventKind(self.kind)
        except ValueError:
            _logger.warning(
                "task_spec_unknown_kind",
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

    @classmethod
    def from_event_and_dispatch(
        cls,
        event: UnifiedEvent,
        dispatch: Dispatch,
        *,
        check_run_id: int | None = None,
        decision_trace: list[dict[str, Any]] | None = None,
        initial_labels: list[str] | None = None,
        classifier_output: dict[str, Any] | None = None,
        stages_to_run: list[str] | None = None,
        is_incremental: bool = False,
        is_force_push: bool = False,
    ) -> TaskSpec:
        now = datetime.now(UTC).isoformat()
        return cls(
            spec_version=TASK_SPEC_VERSION,
            task_id=dispatch.task_id,
            run_id=dispatch.run_id or dispatch.task_id,
            prev_run_id=dispatch.prev_run_id,
            resource_key=dispatch.resource_key,
            event_seq=dispatch.event_seq,
            intent=dispatch.intent or "start",
            enqueued_at=now,
            spec_built_at=now,
            scenario=dispatch.feature.value,
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
            check_run_id=check_run_id,
            decision_trace=decision_trace or [],
            classifier_skipped=(classifier_output is None),
            stages_to_run=stages_to_run or [],
            initial_labels=initial_labels or [],
            classifier_output=classifier_output,
            is_incremental=is_incremental,
            is_force_push=is_force_push,
        )


def deserialize_task_spec(blob: str | bytes) -> TaskSpec | None:
    """Parse v3 TaskSpec blob. Returns None on bad JSON or wrong spec_version."""
    try:
        text = blob.decode("utf-8") if isinstance(blob, (bytes, bytearray)) else blob
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _logger.warning(
            "task_spec_parse_failed",
            extra={"reason": f"{type(exc).__name__}: {exc}"},
        )
        return None
    if not isinstance(data, dict) or data.get("spec_version") != TASK_SPEC_VERSION:
        _logger.warning(
            "task_spec_version_mismatch",
            extra={"spec_version": data.get("spec_version") if isinstance(data, dict) else None},
        )
        return None
    try:
        return TaskSpec(**data)
    except TypeError as exc:
        _logger.warning(
            "task_spec_schema_drift",
            extra={
                "reason": str(exc)[:200],
                "delivery_id": data.get("delivery_id"),
            },
        )
        return None

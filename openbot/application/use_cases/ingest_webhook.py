"""ingest_webhook — application-layer use-case for processing a parsed webhook event.

This module contains all orchestration logic that was previously embedded in the
github_webhook route. The entrypoint (route) becomes thin HTTP glue: parse request,
call ingest_webhook, return response.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openbot.application.router import Dispatch, derive_run_id, dispatch_for, upgrade_dispatch
from openbot.application.state.runs_repo import TransitionResult
from openbot.domain.dedup import DedupOutcome
from openbot.domain.intents import Intent

if TYPE_CHECKING:
    from openbot.application.ports.cancellation import CancellationPort
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.application.ports.dedup import DedupPort
    from openbot.application.ports.queue import QueuePort
    from openbot.application.ports.resource_lock import ResourceLockPort
    from openbot.application.ports.runs_repo import RunsRepoPort
    from openbot.domain.events import UnifiedEvent

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of processing one webhook event.

    ``status`` is one of ``"accepted"``, ``"duplicate"``, or ``"ignored"``.
    """

    status: str  # "accepted" | "duplicate" | "ignored"
    delivery_id: str
    kind: str
    relevant: bool
    feature: str | None = None
    task_id: str | None = None
    entry_id: str | None = None
    check_run_id: int | None = None
    intent: str | None = None
    reason: str | None = None
    resource_key: str | None = None

    def to_response(self) -> dict[str, str | int | bool | None]:
        """Build the HTTP response body (omits None values except check_run_id).

        ``check_run_id`` is always emitted for ``"accepted"`` responses (even
        when None) because downstream consumers and E2E tests rely on the field
        being present in the accepted payload.  All other optional fields are
        omitted when None.
        """
        body: dict[str, str | int | bool | None] = {
            "status": self.status,
            "delivery_id": self.delivery_id,
            "kind": self.kind,
            "relevant": self.relevant,
        }
        # Optional fields — include only when set.
        for key, val in (
            ("feature", self.feature),
            ("task_id", self.task_id),
            ("entry_id", self.entry_id),
            ("intent", self.intent),
            ("reason", self.reason),
            ("resource_key", self.resource_key),
        ):
            if val is not None:
                body[key] = val
        # check_run_id is always present for accepted responses (may be None).
        if self.status == "accepted":
            body["check_run_id"] = self.check_run_id
        return body


# ---------------------------------------------------------------------------
# Private helper — state-machine classification
# ---------------------------------------------------------------------------


async def _classify_and_upgrade(
    *,
    event: UnifiedEvent,
    dispatch: Dispatch,
    runs_repo: RunsRepoPort,
    resource_lock: ResourceLockPort,
) -> tuple[Dispatch, TransitionResult]:
    """Run the state-machine classifier under the per-resource lock.

    Three concerns are bundled here so the use-case stays flat:

      1. ``resource_lock`` keeps two concurrent deliveries for the same
         resource from racing past each other into a double-START.
      2. ``runs_repo.transition`` does the CAS-guarded write under the
         same transaction.
      3. ``upgrade_dispatch`` carries the classifier's intent + run
         identity into the Dispatch so the queue payload and the eventual
         handler invocation see them.

    Returning the ``TransitionResult`` lets the caller branch on
    ``Intent.IGNORE`` without re-reading the row.
    """
    assert event.resource_key is not None  # gated by the caller

    # ``time.monotonic_ns()`` gives a strictly monotonic per-process
    # serial so two simultaneous classified events for the same resource
    # end up with distinct ``run_id`` hashes even when the clock
    # granularity is coarse.
    serial = time.monotonic_ns()
    new_run_id = derive_run_id(event.resource_key, serial)

    async with resource_lock.lock(event.resource_key) as _acquired:
        result = await runs_repo.transition(event=event, new_run_id=new_run_id)

    upgraded = upgrade_dispatch(
        dispatch,
        intent=result.classification.intent.value,
        run_id=result.run_id or new_run_id,
        prev_run_id=result.prev_run_id,
        event_seq=event.event_seq,
        resource_key=event.resource_key,
    )
    return upgraded, result


# ---------------------------------------------------------------------------
# Public use-case entry point
# ---------------------------------------------------------------------------


async def ingest_webhook(
    event: UnifiedEvent,
    adapter: ChannelAdapterPort,
    *,
    dedup: DedupPort,
    runs_repo: RunsRepoPort | None = None,
    resource_lock: ResourceLockPort | None = None,
    cancellation: CancellationPort | None = None,
    queue: QueuePort | None = None,
    github_auth_configured: bool = False,
    redis_client: Any = None,
) -> IngestResult:
    """Process a parsed, authenticated webhook event.

    Callers (the route) are responsible for:
      - Reading the raw body and headers from the HTTP request.
      - Calling ``adapter.verify_signature`` and mapping ``SignatureError``
        to HTTP 401.
      - Calling ``adapter.parse_event`` and mapping errors to HTTP 401.

    This function handles all application-level orchestration:
      1. Dedup check.
      2. Router dispatch.
      3. State-machine classification (when DB + resource_key present).
      4. Cancellation signal.
      5. GitHub check run creation.
      6. Redis enqueue (raises if Redis unavailable).
    """
    # ── 1. Dedup ─────────────────────────────────────────────────────────────
    outcome = await dedup.check_and_mark(event.channel, event.delivery_id)
    if outcome is DedupOutcome.DUPLICATE:
        _logger.info(
            "webhook_duplicate_dropped",
            extra={
                "channel": event.channel,
                "delivery_id": event.delivery_id,
                "kind": event.kind.value,
            },
        )
        return IngestResult(
            status="duplicate",
            delivery_id=event.delivery_id,
            kind=event.kind.value,
            relevant=event.is_relevant,
        )

    _logger.info(
        "webhook_accepted",
        extra={
            "channel": event.channel,
            "delivery_id": event.delivery_id,
            "kind": event.kind.value,
            "repo": event.repo,
            "actor": event.actor,
            "installation_id": event.installation_id,
            "relevant": event.is_relevant,
            "dedup_outcome": outcome.value,
        },
    )

    # ── 2. Router dispatch ────────────────────────────────────────────────────
    dispatch = dispatch_for(event)
    if dispatch is None:
        return IngestResult(
            status="ignored",
            delivery_id=event.delivery_id,
            kind=event.kind.value,
            relevant=event.is_relevant,
        )

    # ── 3. State-machine classification ──────────────────────────────────────
    # Only runs when both Postgres (for task_runs) and a stateful resource_key
    # (issue/PR number, not ping/push) are present. The two-tier guard means
    # dev mode without docker-compose and integration tests without a DB still
    # exercise the original v1 path without crashing.
    transition_result: TransitionResult | None = None
    if runs_repo is not None and resource_lock is not None and event.resource_key is not None:
        try:
            dispatch, transition_result = await _classify_and_upgrade(
                event=event,
                dispatch=dispatch,
                runs_repo=runs_repo,
                resource_lock=resource_lock,
            )
        except Exception:
            # State-machine path failed (DB unreachable, etc.). Audit and fall
            # back to the v1 path so we never drop a webhook due to a Postgres
            # flap.
            _logger.exception(
                "state_machine_transition_failed",
                extra={"delivery_id": event.delivery_id, "repo": event.repo},
            )
            transition_result = None
        else:
            # IGNORE → no work to enqueue; the row (if any) was already
            # written under the lock. Still return 202 to GitHub.
            if transition_result.classification.intent is Intent.IGNORE:
                _logger.info(
                    "state_machine_ignored",
                    extra={
                        "delivery_id": event.delivery_id,
                        "resource_key": event.resource_key,
                        "reason": transition_result.classification.reason,
                    },
                )
                return IngestResult(
                    status="ignored",
                    delivery_id=event.delivery_id,
                    kind=event.kind.value,
                    relevant=event.is_relevant,
                    intent=transition_result.classification.intent.value,
                    reason=transition_result.classification.reason,
                    resource_key=event.resource_key,
                )

            # SUPERSEDE / CANCEL: signal the prior run so workers on any dyno
            # can stop early at their next checkpoint. We signal from the
            # receive side too (in addition to the worker doing it on dequeue)
            # so even a long queue lag can't delay the cancel.
            if transition_result.prev_run_id and cancellation is not None:
                try:
                    await cancellation.signal(transition_result.prev_run_id)
                except Exception:
                    _logger.exception(
                        "cancellation_signal_failed",
                        extra={
                            "delivery_id": event.delivery_id,
                            "prev_run_id": transition_result.prev_run_id,
                        },
                    )

    # ── 4. Check run creation ─────────────────────────────────────────────────
    # Immediate feedback (GitHub Check Run) for PR events. We create the check
    # run synchronously in the webhook handler so the check_run_id is available
    # for the worker's updates.
    check_run_id: int | None = None
    if event.pr_number and github_auth_configured:
        head_sha = (event.raw.get("pull_request") or {}).get("head", {}).get("sha")
        if head_sha:
            try:
                check_run = await adapter.create_check_run(  # type: ignore[attr-defined]
                    event,
                    name="OpenBot Analysis",
                    head_sha=head_sha,
                    output={
                        "title": "Starting Analysis...",
                        "summary": (
                            f"OpenBot has received delivery `{event.delivery_id}` and is "
                            f"dispatching the `{dispatch.feature.value}` workflow."
                        ),
                    },
                )
                check_run_id = check_run.get("id")
            except Exception:
                # Failing to create a check run shouldn't stop workflow dispatch.
                _logger.exception(
                    "check_run_creation_failed",
                    extra={"delivery_id": event.delivery_id, "repo": event.repo},
                )

    # ── 5. Enqueue to Redis Stream ────────────────────────────────────────────
    # Redis is required. If enqueue fails, raise so the route returns 500
    # and GitHub retries the webhook (GitHub retries with exponential backoff
    # for up to several days).
    if redis_client is None:
        raise RuntimeError("Redis client is not configured — webhook dispatch requires Redis")

    assert queue is not None, "queue port must be set when redis_client is present"

    # ── 5a. LLM classifier (fail-open) ───────────────────────────────────────
    # Run *before* enqueuing so the worker receives pre-computed classifier
    # output and doesn't need a second LLM round-trip. ``classify_for_dispatch``
    # is fail-open: any exception returns None, which ``derive_sandbox_policy``
    # treats as "respect the static SandboxPolicy". ``classifier_skipped`` in
    # TaskSpec tracks whether the output was produced or not.
    from dataclasses import asdict

    from openbot.dispatcher.classifier import classify_for_dispatch

    _classifier_output = await classify_for_dispatch(
        event=event,
        feature=dispatch.feature,
        redis=redis_client,
    )
    classifier_output_dict = asdict(_classifier_output) if _classifier_output is not None else None

    from openbot.infrastructure.queue.task_spec import TaskSpec

    spec = TaskSpec.from_event_and_dispatch(
        event,
        dispatch,
        check_run_id=check_run_id,
        classifier_output=classifier_output_dict,
    )
    entry_id = await queue.enqueue_task_spec(spec)
    return IngestResult(
        status="accepted",
        delivery_id=event.delivery_id,
        kind=event.kind.value,
        relevant=event.is_relevant,
        feature=dispatch.feature.value,
        task_id=dispatch.task_id,
        entry_id=entry_id,
        check_run_id=check_run_id,
    )


__all__ = ["IngestResult", "ingest_webhook"]

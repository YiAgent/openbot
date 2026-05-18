"""POST /webhook/github — signed webhook ingestion."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from openbot.application.dispatcher import run_dispatch
from openbot.application.router import Dispatch, derive_run_id, dispatch_for, upgrade_dispatch
from openbot.application.state.runs_repo import TransitionResult
from openbot.domain.intents import Intent
from openbot.infrastructure.adapters.base import SignatureError
from openbot.infrastructure.adapters.github import GitHubAdapter
from openbot.infrastructure.persistence import DedupOutcome, WebhookDedup
from openbot.infrastructure.queue.payload import QueuePayload

if TYPE_CHECKING:
    from openbot.application.ports.resource_lock import ResourceLockPort
    from openbot.application.ports.runs_repo import RunsRepoPort
    from openbot.domain.events import UnifiedEvent

_logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request, background: BackgroundTasks
) -> dict[str, str | int | bool | None]:
    """Receive a GitHub webhook.

    Order matters (PRD §5.1):
      1. read RAW body bytes — HMAC is computed over them
      2. verify signature (constant-time)
      3. parse → UnifiedEvent
      4. dedup via Redis SET NX EX — duplicate retries short-circuit with 202
      5. schedule workflow as a background task (runs after we 202)
      6. return 202 immediately so GitHub doesn't retry

    BackgroundTasks remains the v0.1 stop-gap; a Redis queue + delivery_id
    keyed worker lands in the middleware slice so workflows survive a
    process restart.
    """
    adapter: GitHubAdapter | None = getattr(request.app.state, "github_adapter", None)
    if adapter is None:
        # Webhooks intentionally off until setup.sh has run.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OPENBOT_GITHUB_WEBHOOK_SECRET is not set",
        )

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        adapter.verify_signature(body, headers)
    except SignatureError as exc:
        # Audit: log the failed delivery_id so attacker probes are visible.
        _logger.warning(
            "webhook_signature_rejected",
            extra={"delivery_id": headers.get("x-github-delivery", "?"), "reason": str(exc)},
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    try:
        event = adapter.parse_event(body, headers)
    except SignatureError as exc:
        # parse_event raises SignatureError for "valid HMAC + invalid JSON"
        # by design (github.py docstring): a passer-by who can sign but
        # sends garbage is a more pointed probe than one who can't sign at
        # all, so we collapse it into the same 401 lane rather than 500.
        # Distinct log key so dashboards can separate the two failure
        # modes — alert on bad-JSON-after-good-HMAC differently than
        # plain HMAC-mismatch noise.
        _logger.warning(
            "webhook_payload_unparseable",
            extra={"delivery_id": headers.get("x-github-delivery", "?"), "reason": str(exc)},
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    # Dedup: if GitHub re-delivers the same X-GitHub-Delivery (our slow handler
    # or any 5xx makes them retry), we still 202 but skip workflow dispatch.
    # The dedup falls open when Redis is unconfigured/down — see WebhookDedup docstring.
    dedup: WebhookDedup = request.app.state.dedup
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
        return {
            "status": "duplicate",
            "delivery_id": event.delivery_id,
            "kind": event.kind.value,
            "relevant": event.is_relevant,
        }

    # Structured audit log — never include body / token / actor email.
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

    # Router → workflow dispatch (harness spec §3 M2).
    # `dispatch_for` is pure: no GitHub API calls, no DB. If the event
    # has no matching handler (e.g. push, star, bot author) we still
    # 202 — GitHub only needs to know we received the delivery.
    dispatch = dispatch_for(event)
    if dispatch is None:
        return {
            "status": "ignored",
            "delivery_id": event.delivery_id,
            "kind": event.kind.value,
            "relevant": event.is_relevant,
        }

    # State-machine classification: only runs when both Postgres (for
    # ``task_runs``) and a stateful resource_key (issue/PR number, not
    # ping/push) are present. The two-tier guard means dev mode
    # without docker-compose and integration tests without a DB still
    # exercise the original v1 path without crashing.
    runs_repo = getattr(request.app.state, "runs_repo", None)
    redis_client = getattr(request.app.state, "redis", None)
    cancellation = getattr(request.app.state, "cancellation", None)
    transition_result: TransitionResult | None = None
    if runs_repo is not None and event.resource_key is not None:
        try:
            dispatch, transition_result = await _classify_and_upgrade(
                event=event,
                dispatch=dispatch,
                runs_repo=runs_repo,
                resource_lock=request.app.state.resource_lock,
            )
        except Exception:
            # State-machine path failed (DB unreachable, etc.). Audit
            # and fall back to the v1 path so we never drop a webhook
            # because of a Postgres flap.
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
                return {
                    "status": "ignored",
                    "delivery_id": event.delivery_id,
                    "kind": event.kind.value,
                    "intent": transition_result.classification.intent.value,
                    "reason": transition_result.classification.reason,
                    "resource_key": event.resource_key,
                }
            # SUPERSEDE / CANCEL: signal the prior run so workers on
            # any dyno can stop early at their next checkpoint. We
            # signal from the receive side too (in addition to the
            # worker doing it on dequeue) so even a long queue lag
            # can't delay the cancel.
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

    # Immediate feedback (GitHub Check Run) for PR events.
    # We create the check run synchronously in the webhook handler so the
    # check_run_id is available for the worker's updates.
    check_run_id: int | None = None
    if event.pr_number and request.app.state.github_auth is not None:
        # Extract head_sha from the PR payload.
        head_sha = (event.raw.get("pull_request") or {}).get("head", {}).get("sha")
        if head_sha:
            try:
                check_run = await adapter.create_check_run(
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
                # Failing to create a check run shouldn't stop the workflow dispatch.
                _logger.exception(
                    "check_run_creation_failed",
                    extra={"delivery_id": event.delivery_id, "repo": event.repo},
                )

    # Hand off to the worker queue if Redis is configured; otherwise
    # fall back to FastAPI BackgroundTasks (dev / unit tests).
    #
    # The queue path is what production runs — workflows survive a
    # webapp restart because the entry persists in Redis until a
    # worker XACKs it. The fallback exists so `make dev` without
    # docker-compose still gives a working bot.
    # ``redis_client`` was already read above for the state-machine
    # path; reuse it here so we don't double-tap ``app.state``.
    if redis_client is not None:
        try:
            payload = QueuePayload.from_event(
                event,
                feature=dispatch.feature,
                task_id=dispatch.task_id,
                check_run_id=check_run_id,
                intent=dispatch.intent,
                run_id=dispatch.run_id,
                prev_run_id=dispatch.prev_run_id,
                resource_key=dispatch.resource_key,
                event_seq=dispatch.event_seq,
            )
            entry_id = await request.app.state.queue.enqueue(payload)
            return {
                "status": "accepted",
                "delivery_id": event.delivery_id,
                "kind": event.kind.value,
                "feature": dispatch.feature.value,
                "task_id": dispatch.task_id,
                "entry_id": entry_id,
                "relevant": event.is_relevant,
                "check_run_id": check_run_id,
            }
        except Exception:
            # Redis enqueue failed (RDB save in progress, OOM, etc.).
            # Don't 5xx the webhook — degrade to BackgroundTask so the
            # event isn't lost. The next event will retry the queue path.
            _logger.exception(
                "queue_enqueue_failed_falling_back_to_background_task",
                extra={"delivery_id": event.delivery_id, "repo": event.repo},
            )

    background.add_task(
        _run_dispatch,
        request.app,
        adapter,
        event,
        dispatch,
        check_run_id,
    )

    return {
        "status": "accepted",
        "delivery_id": event.delivery_id,
        "kind": event.kind.value,
        "feature": dispatch.feature.value,
        "task_id": dispatch.task_id,
        "relevant": event.is_relevant,
        "check_run_id": check_run_id,
    }


async def _classify_and_upgrade(
    *,
    event: UnifiedEvent,
    dispatch: Dispatch,
    runs_repo: RunsRepoPort,
    resource_lock: ResourceLockPort,
) -> tuple[Dispatch, TransitionResult]:
    """Run the state-machine classifier under the per-resource lock.

    Three concerns are bundled here so the webhook handler stays
    flat:

      1. ``resource_lock`` keeps two concurrent deliveries for the
         same resource from racing past each other into a double-START.
      2. ``runs_repo.transition`` does the CAS-guarded write under
         the same transaction.
      3. ``upgrade_dispatch`` carries the classifier's intent +
         run identity into the Dispatch so the queue payload and the
         eventual handler invocation see them.

    The function commits the session on success — a single transaction
    boundary covers both the read-prior-row and the write-new-row so
    that the SQLAlchemy ``version_id_col`` CAS check sees the same
    snapshot. Returning the ``TransitionResult`` lets the caller
    branch on ``Intent.IGNORE`` without re-reading the row.
    """
    assert event.resource_key is not None  # gated by the caller
    # ``time.monotonic_ns()`` gives a strictly monotonic per-process
    # serial so two simultaneous classified events for the same
    # resource end up with distinct ``run_id`` hashes even when the
    # clock granularity is coarse.
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


async def _run_dispatch(
    app_instance: object,
    adapter: GitHubAdapter,
    event: UnifiedEvent,
    dispatch: Dispatch,
    check_run_id: int | None = None,
) -> None:
    """In-process dispatch — used as the fallback when Redis is absent.

    Production runs through the Redis queue worker (``openbot.infrastructure.queue.runner``)
    instead; this path exists so `make dev` without docker-compose still
    delivers a working bot and so unit tests don't need fakeredis just
    to exercise the webhook flow.

    The actual middleware chain + handler invocation lives in
    ``openbot.application.dispatcher.run_dispatch`` so the worker and the webapp can't
    drift apart.
    """
    await run_dispatch(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        session_factory=getattr(app_instance.state, "db_session_factory", None),
        redis=getattr(app_instance.state, "redis", None),
        check_run_id=check_run_id,
    )

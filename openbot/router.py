"""Router — UnifiedEvent → (Feature, workflow handler, task_id).

Harness spec §3 M2 / §9.1.

This is the **input-side** Router only. It maps an authenticated webhook
event to the workflow stub that should handle it; it does **not** decide
whether the workflow may proceed. Authorization / budget / rate-limit
gating happens in the pre-flight middleware chain (`openbot.middleware`).

Pure, no I/O. `dispatch_for(event)` returns `None` for events the bot
doesn't react to — the webapp short-circuits with 202 in that case.

`task_id` derivation is locked at SHA-256 of `"{channel}|{repo}|{delivery_id}"`
truncated to 32 hex chars (harness spec §9.1):

  - **Deterministic** so a webhook retry produces the same task_id and
    the existing cost_meter row is reused — never double-charge.
  - **128-bit** so the `cost_meter.task_id VARCHAR(64)` column never
    overflows; collision probability is far below the lifetime
    workload of a single OpenBot instance.
  - **Hex** so log greps stay readable.

State-machine slice (this revision):

  - ``derive_run_id`` returns a per-resource id keyed on the
    ``resource_key`` + a caller-supplied serial. The receive side
    allocates the serial via ``time.monotonic_ns()`` so two
    simultaneous classified events for the same resource end up with
    distinct run_ids.
  - ``Dispatch`` carries optional v2 fields (``intent`` / ``run_id`` /
    ``prev_run_id`` / ``event_seq`` / ``resource_key``); legacy
    callers that build a v1 ``Dispatch`` (router unit tests, the
    in-process BackgroundTask path) leave them ``None`` and the
    handler-side code degrades gracefully.
  - When ``settings.debug_echo_enabled`` is True, every classified
    feature routes to ``handlers.debug_echo`` instead of the real
    workflow stub — see ``openbot.handlers.debug_echo`` for the trace
    schema.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from openbot.config import get_settings
from openbot.domain.identifiers import derive_run_id, derive_task_id
from openbot.events import EventKind, UnifiedEvent
from openbot.llm.router import Feature

if TYPE_CHECKING:
    # Avoid the import-cycle: workflows import middleware → middleware
    # imports nothing from router. We only need the handlers' callable
    # signature for type-checking.
    from openbot.middleware.preflight import PreflightContext

    Handler = Callable[[PreflightContext], Awaitable[None]]
else:
    Handler = Callable[..., Awaitable[None]]


# `@openbot ` chat-mention prefix. Trailing space mandatory so as not to match
# other bot logins starting with the same prefix.
_CHAT_PREFIX_DEFAULT: Final = "@openbot "


def _get_chat_prefix() -> str:
    """Return the @handle mention prefix.

    Default is '@openbot ', but in production we prefer the actual
    App handle (e.g. '@yibots ') if available.
    """
    # Slice C/D will plumb the authenticated bot handle here via Settings.
    # To keep existing E2E tests green, the default must be '@openbot '.
    return _CHAT_PREFIX_DEFAULT


@dataclass(frozen=True, slots=True)
class Dispatch:
    """Result of `dispatch_for` — what to run, under what feature, with what id.

    State-machine fields are optional so the legacy in-process
    BackgroundTask path (no classifier in front of it) still produces
    a valid Dispatch. The webapp/worker upgrade-augment the dispatch
    with the classifier's decision before invoking the handler.
    """

    feature: Feature
    handler: Handler
    task_id: str
    # v2 fields — populated by the receive side after running the
    # state-machine classifier. None on the in-process fallback path.
    intent: str | None = None
    run_id: str | None = None
    prev_run_id: str | None = None
    event_seq: int = 0
    resource_key: str | None = None


def _resolve_handler(feature: Feature) -> Handler:
    """Pick the real workflow handler or the debug echo handler.

    The decision is made per-call so toggling ``OPENBOT_DEBUG_ECHO`` at
    runtime (e.g. via ``heroku config:set``) takes effect on the next
    webhook without a process restart. The import is lazy so this
    module stays free of side-effects at import time.
    """
    if get_settings().debug_echo_enabled:
        from openbot.handlers.debug_echo import debug_echo_handler

        return debug_echo_handler

    from openbot.workflows import maybe_run_chat, maybe_run_fix, maybe_run_review, maybe_run_triage

    if feature is Feature.TRIAGE:
        return maybe_run_triage
    if feature is Feature.REVIEW:
        return maybe_run_review
    if feature is Feature.FIX:
        return maybe_run_fix
    if feature is Feature.CHAT:
        return maybe_run_chat
    # Defensive: any new Feature variant that lands here would have to
    # be added to the workflow imports. Returning a no-op coroutine
    # would mask that, so we raise instead.
    raise RuntimeError(f"No handler wired for feature={feature.value}")


def dispatch_for(event: UnifiedEvent) -> Dispatch | None:
    """Map an event to its workflow handler.

    Returns `None` when the bot has nothing to do — the webapp still
    returns 202 to GitHub, but no background task is scheduled.

    The handlers are imported lazily (inside the function) so this
    module stays free of side-effects at import time and tests can
    monkeypatch the workflow modules cheaply.
    """
    # Bots never trigger workflows. is_from_bot catches dependabot,
    # coderabbit, our own openbot[bot] reply, etc. Avoids echo loops
    # before we even reach pre-flight.
    if event.is_from_bot:
        return None

    if event.kind is EventKind.UNKNOWN:
        return None

    task_id = derive_task_id(event)

    if event.kind in (EventKind.ISSUE_OPENED, EventKind.ISSUE_EDITED, EventKind.ISSUE_REOPENED):
        if event.issue_number is None or event.installation_id is None:
            # PRD §5.1 promises both for authentic issue events; bail
            # rather than schedule a workflow that will crash later.
            return None
        return Dispatch(Feature.TRIAGE, _resolve_handler(Feature.TRIAGE), task_id)

    if event.kind in (EventKind.PR_OPENED, EventKind.PR_SYNCHRONIZED, EventKind.PR_REOPENED):
        if event.pr_number is None or event.installation_id is None:
            return None
        return Dispatch(Feature.REVIEW, _resolve_handler(Feature.REVIEW), task_id)

    if event.kind is EventKind.ISSUE_ASSIGNED:
        # PRD §4.3 trigger: `issue.assignees` must include the bot.
        # Without this check, every human↔human assignment fires
        # `maybe_run_fix` and posts a "agent will start shortly" ACK —
        # spammy and confusing. We check `assignee.type == "Bot"` on
        # the just-assigned actor; that's correct for v0.1 (only the
        # bot is a Bot assignee in practice — humans don't assign
        # dependabot/etc. to issues for fixing). Slice C will tighten
        # this to "is our App's specific bot login" once the App's
        # own login is plumbed through.
        assignee = (event.raw.get("assignee") or {}) if event.raw else {}
        if assignee.get("type") != "Bot":
            return None
        if event.issue_number is None or event.installation_id is None:
            return None
        return Dispatch(Feature.FIX, _resolve_handler(Feature.FIX), task_id)

    if event.kind in (
        EventKind.ISSUE_COMMENT_CREATED,
        EventKind.PR_REVIEW_COMMENT_CREATED,
    ):
        body = event.comment_body or ""
        prefix = _get_chat_prefix()
        if not body.startswith(prefix) and not body.startswith("@yibots "):
            return None
        if event.installation_id is None:
            return None
        return Dispatch(Feature.CHAT, _resolve_handler(Feature.CHAT), task_id)

    if event.kind in (EventKind.ISSUE_LABELED, EventKind.ISSUE_UNLABELED):
        if event.issue_number is None or event.installation_id is None:
            return None
        return Dispatch(Feature.TRIAGE, _resolve_handler(Feature.TRIAGE), task_id)

    if event.kind in (EventKind.PR_LABELED, EventKind.PR_UNLABELED):
        if event.pr_number is None or event.installation_id is None:
            return None
        return Dispatch(Feature.REVIEW, _resolve_handler(Feature.REVIEW), task_id)

    return None


def upgrade_dispatch(
    dispatch: Dispatch,
    *,
    intent: str,
    run_id: str,
    prev_run_id: str | None,
    event_seq: int,
    resource_key: str,
) -> Dispatch:
    """Return a new ``Dispatch`` with the v2 state-machine fields populated.

    Receive side calls this AFTER ``runs_repo.transition`` so the
    Dispatch carries the classified intent + run identity into the
    payload + handler. Kept as a separate constructor so ``dispatch_for``
    stays pure (no DB) and trivially unit-testable.
    """
    return Dispatch(
        feature=dispatch.feature,
        handler=dispatch.handler,
        # task_id stays as the original delivery-derived id so cost_meter
        # rows produced by retries of the same delivery still upsert
        # onto the same key. The run_id is the per-resource id the
        # state machine cares about.
        task_id=dispatch.task_id,
        intent=intent,
        run_id=run_id,
        prev_run_id=prev_run_id,
        event_seq=event_seq,
        resource_key=resource_key,
    )


__all__ = [
    "Dispatch",
    "derive_run_id",
    "derive_task_id",
    "dispatch_for",
    "upgrade_dispatch",
]

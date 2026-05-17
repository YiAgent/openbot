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
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

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
    """Result of `dispatch_for` — what to run, under what feature, with what id."""

    feature: Feature
    handler: Handler
    task_id: str


def derive_task_id(event: UnifiedEvent) -> str:
    """Stable 128-bit hex task_id (harness spec §9.1)."""
    material = f"{event.channel}|{event.repo}|{event.delivery_id}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


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

    from openbot.workflows import maybe_run_chat, maybe_run_fix, maybe_run_review, maybe_run_triage

    if event.kind is EventKind.ISSUE_OPENED:
        if event.issue_number is None or event.installation_id is None:
            # PRD §5.1 promises both for authentic issue events; bail
            # rather than schedule a workflow that will crash later.
            return None
        return Dispatch(Feature.TRIAGE, maybe_run_triage, derive_task_id(event))

    if event.kind in (EventKind.PR_OPENED, EventKind.PR_SYNCHRONIZED):
        if event.pr_number is None or event.installation_id is None:
            return None
        return Dispatch(Feature.REVIEW, maybe_run_review, derive_task_id(event))

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
        return Dispatch(Feature.FIX, maybe_run_fix, derive_task_id(event))

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
        return Dispatch(Feature.CHAT, maybe_run_chat, derive_task_id(event))

    return None

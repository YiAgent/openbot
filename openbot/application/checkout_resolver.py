"""``resolve_checkout`` — pure-ish ref resolution for the unified sandbox.

Given a ``UnifiedEvent`` and the ``Workflow`` the dispatcher is about
to run, return the exact ``CheckoutSpec`` the sandbox should clone:
which ``ref`` (always a concrete SHA, never a branch name), which
``CloneStrategy``, and — for review workflows — the ``diff_base`` SHA
to compute the diff against.

This is the single highest-risk surface in the slice: a bug here means
the agent reads the wrong code at the wrong commit and gives the user
a wrong answer. The resolver lives in ``application/`` (not
``infrastructure/``) so that all I/O it does is mediated through
``ChannelAdapterPort`` — concrete adapters can be swapped (GitHub,
fake) without touching this file.

The full matrix is in ``docs/superpowers/specs/
2026-05-21-unified-sandbox-entry-design.md`` § "The ref-resolution
matrix". Every cell in that matrix has a unit test in
``tests/application/test_checkout_resolver.py``.

Failure mode: every branch either returns a ``CheckoutSpec`` or raises
``CheckoutResolutionError``. The dispatcher catches the error and falls
into the graceful-degrade path (handler runs with ``sandbox_handle is
None``). There is no silent default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openbot.domain.checkout import (
    CheckoutResolutionError,
    CheckoutSpec,
    CloneStrategy,
)
from openbot.domain.events import EventKind
from openbot.domain.workflows import Workflow

if TYPE_CHECKING:
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.domain.events import UnifiedEvent


# Per-workflow default ``CloneStrategy`` (spec § "The ref-resolution
# matrix"). The dictionary lives at module scope (not inside the
# resolver) so callers and tests can introspect it; it's a frozen
# mapping in spirit — the resolver only ever *reads* from it.
_STRATEGY_BY_WORKFLOW: dict[Workflow, CloneStrategy] = {
    Workflow.TRIAGE: CloneStrategy.SHALLOW,
    Workflow.REVIEW: CloneStrategy.SHALLOW_HISTORY,
    Workflow.FIX: CloneStrategy.SHALLOW,
    Workflow.CHAT: CloneStrategy.BLOBLESS,
}


def _strategy_for(workflow: Workflow) -> CloneStrategy:
    """Default clone strategy for ``workflow``.

    Split out as a helper so the resolver's branches read as
    "checkout this ref with the workflow's default strategy" — the
    branch is about *which ref*, not about strategy selection.
    """
    return _STRATEGY_BY_WORKFLOW[workflow]


async def resolve_checkout(
    event: UnifiedEvent,
    workflow: Workflow,
    adapter: ChannelAdapterPort,
) -> CheckoutSpec:
    """Resolve the ``CheckoutSpec`` for a ``(event, workflow)`` pair.

    Raises ``CheckoutResolutionError`` if no rule matches — the caller
    catches and degrades to "no sandbox".

    See module docstring for the full failure-mode contract.
    """
    if event.clone_url is None:
        # Without a clone URL we can't checkout *anything*. Adapters
        # are responsible for populating this on every payload that
        # could trigger a workflow — if we land here, the adapter
        # has a bug. Raising rather than synthesizing a URL keeps the
        # contract honest.
        raise CheckoutResolutionError(f"event missing clone_url: kind={event.kind.value}")

    # ── Highest-specificity rule first ───────────────────────────────
    # Inline review comments carry a ``commit_id`` that pins the exact
    # commit the reviewer was looking at. We check this *before* the
    # generic PR branch because (a) it avoids an extra adapter call
    # and (b) using ``pr.head.sha`` here would silently drift if the
    # PR got new commits between comment-receipt and our resolution.
    if event.kind is EventKind.PR_REVIEW_COMMENT_CREATED and event.review_commit_id is not None:
        return CheckoutSpec(
            repo_url=event.clone_url,
            ref=event.review_commit_id,
            strategy=_strategy_for(workflow),
        )

    # ── PR-context dispatch ──────────────────────────────────────────
    # Covers PR_OPENED / PR_SYNCHRONIZED / PR_REOPENED (review), as
    # well as ISSUE_COMMENT_CREATED on a PR (chat). One adapter call
    # to fetch head/base SHAs the webhook payload doesn't carry.
    if event.pr_number is not None:
        pr = await adapter.get_pull_request(event, event.pr_number)
        head_sha = str(pr["head"]["sha"])
        base_sha = str(pr["base"]["sha"])

        if workflow is Workflow.REVIEW:
            # Incremental review: if we've reviewed an earlier SHA on
            # this PR, diff against *that* — otherwise fall back to the
            # PR's base. ``last_reviewed_sha`` comes from the dispatch
            # state DB (hydrated by the dispatcher before calling us).
            return CheckoutSpec(
                repo_url=event.clone_url,
                ref=head_sha,
                strategy=CloneStrategy.SHALLOW_HISTORY,
                diff_base=event.last_reviewed_sha or base_sha,
            )
        return CheckoutSpec(
            repo_url=event.clone_url,
            ref=head_sha,
            strategy=_strategy_for(workflow),
        )

    # ── Issue-context dispatch ───────────────────────────────────────
    # Covers ISSUE_OPENED / ISSUE_ASSIGNED / ISSUE_LABELED (triage,
    # fix) and ISSUE_COMMENT_CREATED on a plain issue (chat). Every
    # branch resolves to the repo's current default-branch HEAD.
    if event.issue_number is not None:
        default_sha = await adapter.get_default_branch_sha(event)
        return CheckoutSpec(
            repo_url=event.clone_url,
            ref=default_sha,
            strategy=_strategy_for(workflow),
        )

    # No context: this should have been gated by ``SandboxPolicy.
    # NO_SANDBOX`` at the router. Reaching here is a contract violation
    # somewhere upstream; raise so the dispatcher can degrade.
    raise CheckoutResolutionError(
        f"no resolution rule for kind={event.kind.value} workflow={workflow.value}"
    )


__all__ = ["resolve_checkout"]

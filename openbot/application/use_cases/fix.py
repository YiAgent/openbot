"""Issue → PR fix workflow — PRD §4.3 end-to-end pipeline.

Flow (Part-3 post-migration shape):
  1. Fetch the issue body from GitHub (default branch / base SHA come
     from the same payload).
  2. Consume the pre-provisioned ``ctx.sandbox_handle`` —
     ``dispatcher._run_with_sandbox`` already opened the sandbox,
     fetched the installation token, resolved the ``CheckoutSpec``,
     and cloned at ``checkout.ref``. The handler **never** clones.
  3. Run ``DeepAgentsFixResponder`` to produce a ``FixOutcome``.
  4. Tests passed → push branch, open PR, comment with PR URL.
     Tests failed → comment with the truncated test output.
     Any step raised → comment with the corresponding error template.

If ``ctx.sandbox_handle is None`` the handler posts the ``_NO_SANDBOX``
degrade reply and returns — the dispatcher already logged the cause
(factory missing / clone failed / resolution failed) under
``openbot_dispatch_sandbox_total{bypass_source="degrade"}``.

The whole pipeline runs inside ``audit_lifecycle`` so the workflow
phase transitions get logged uniformly with review/triage. No path
raises out of this function — every failure becomes a comment.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from openbot.application.state.cancellation import checkpoint
from openbot.application.use_cases._lifecycle import audit_lifecycle
from openbot.application.use_cases._tracing import observe as _observe
from openbot.application.use_cases._tracing import traceable as _traceable
from openbot.domain.fix import FixOutcome
from openbot.domain.workflows import Workflow
from openbot.infrastructure.agents import DeepAgentsFixResponder

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from openbot.application.middleware.preflight import PreflightContext
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.domain.events import UnifiedEvent

_logger = logging.getLogger(__name__)

# GitHub comments hard-cap at 65_536 chars. 4_000 keeps headers + URLs
# + diff snippets fitting when the agent emits verbose test output.
_MAX_TEST_OUTPUT_CHARS = 4_000

_NO_SANDBOX = (
    ":robot: OpenBot can't run the fix loop: the sandbox is not configured "
    "on this deployment. The maintainer needs to set "
    "`OPENBOT_DAYTONA_API_KEY` (or another sandbox backend) before "
    "automated fixes will work."
)
_ISSUE_READ_FAIL = (
    ":warning: OpenBot could not read this issue from GitHub. The fix loop "
    "was skipped. The error has been logged."
)
# Part 3: ``_CLONE_FAIL`` was deleted with the internal clone block.
# The dispatcher now owns clone + token errors — both surface to the
# user as ``_NO_SANDBOX`` (the dispatcher leaves ``sandbox_handle =
# None`` on its degrade path) and are tagged on the
# ``dispatch_sandbox_total{bypass_source="degrade"}`` counter so ops
# can still split the causes in dashboards.
_AGENT_FAIL = (
    ":warning: OpenBot's fix agent failed before producing a result. "
    "The error has been logged; please re-assign the issue to retry."
)
_BRANCH_CONFLICT = (
    ":warning: OpenBot finished the fix locally but could not create the "
    "branch on GitHub (it may already exist). The error has been logged."
)
_PUSH_FAIL = (
    ":warning: OpenBot finished the fix locally but could not push the "
    "branch to GitHub. The error has been logged."
)
_OPEN_PR_FAIL = (
    ":warning: OpenBot pushed the fix branch but could not open the pull "
    "request. The error has been logged."
)
_TESTS_FAILED_HEADER = (
    ":x: OpenBot attempted a fix but the tests did not pass.\n\n"
    "**Summary:** {summary}\n"
    "**Command:** `{cmd}`\n\n"
    "<details><summary>Test output</summary>\n\n```\n{output}\n```\n</details>"
)
_PR_OPENED = (
    ":sparkles: OpenBot opened a fix for issue #{issue}: {url}\n\n"
    "**Summary:** {summary}\n"
    "**Files changed:** {files}\n"
    "**Test command:** `{cmd}`"
)


def _truncate(text: str, *, limit: int = _MAX_TEST_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return text[:limit] + f"\n\n[truncated — {dropped} bytes of test output omitted]"


def _short_sha(sha: str) -> str:
    return sha[:7] if sha else "nosha"


def _branch_name(*, issue_number: int, base_sha: str) -> str:
    return f"openbot/fix-issue-{issue_number}-{_short_sha(base_sha)}"


def _pr_title(issue_title: str, issue_number: int) -> str:
    head = (issue_title or "").strip().replace("\n", " ")
    if not head:
        head = f"Fix for issue #{issue_number}"
    # GitHub caps PR titles at 256 chars; 240 leaves headroom for the
    # ``[OpenBot] `` prefix (10 chars) plus a small ellipsis margin so
    # truncation never lands mid-emoji or mid-issue-number.
    return f"[OpenBot] {head}"[:240]


def _pr_body(*, attempt_summary: str, issue_number: int, test_command: str) -> str:
    return (
        f"Closes #{issue_number}.\n\n"
        f"**Summary:** {attempt_summary}\n"
        f"**Test command:** `{test_command}`\n\n"
        "_Generated by OpenBot. Reviewer should still read the diff before merging._"
    )


def _files_changed_str(files: tuple[str, ...]) -> str:
    if not files:
        return "(none reported)"
    if len(files) <= 5:
        return ", ".join(f"`{f}`" for f in files)
    head = ", ".join(f"`{f}`" for f in files[:5])
    return f"{head}, …and {len(files) - 5} more"


async def _generate_fix_outcome(
    *,
    sandbox: SandboxPort,
    event: UnifiedEvent,
    adapter: ChannelAdapterPort,
    issue: dict[str, Any],
    run_id: str | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> FixOutcome:
    """Module-level seam — E2E tests monkeypatch this to skip DeepAgents.

    Mirrors ``_generate_review_findings`` in ``review.py``. Keeping the
    responder construction inside a top-level coroutine means tests can
    replace just this one function without touching the orchestration
    code that wraps it.
    """
    responder = DeepAgentsFixResponder()
    return await responder.fix_for_event(
        event,
        adapter=adapter,
        sandbox=sandbox,
        issue=issue,
        run_id=run_id,
        checkpointer=checkpointer,
    )


@_observe(name="fix", capture_input=False)
@_traceable(run_type="chain", name="fix")
async def maybe_run_fix(ctx: PreflightContext) -> None:
    event = ctx.event
    if event.issue_number is None or event.installation_id is None:
        _logger.info(
            "fix_skipped_missing_context",
            extra={"delivery_id": event.delivery_id, "kind": event.kind.value},
        )
        return

    adapter = ctx.adapter
    issue_number = event.issue_number
    run_id = ctx.dispatch.run_id
    checkpointer = ctx.agent_checkpointer

    # Unified-entry contract: the dispatcher either pre-provisions a
    # SandboxedHandle (sandbox + checkout + token, already cloned) or
    # sets ``sandbox_handle = None`` on the degrade path. The cause of
    # the degrade (factory missing / resolver / clone / token) is
    # already counted by ``openbot_dispatch_sandbox_total`` — we just
    # post the user-visible reply here.
    if ctx.sandbox_handle is None:
        await _safe_reply(adapter, event, _NO_SANDBOX)
        return

    handle = ctx.sandbox_handle
    sandbox = handle.sandbox
    token = handle.token
    # ``checkout.ref`` is the concrete SHA the dispatcher cloned at, so
    # the branch name lines up with the workspace HEAD even if the
    # adapter's issue payload disagrees (defence-in-depth — the
    # resolver and the adapter both look at the same ``base_sha`` in
    # v0.1, but coupling on the handle makes the dependency explicit).
    base_sha = handle.checkout.ref

    try:
        async with audit_lifecycle(ctx, workflow=Workflow.FIX) as audit:
            try:
                issue = await adapter.get_issue(event, issue_number)
            except Exception:
                _logger.exception("fix_get_issue_failed", extra=_log_extra(event))
                await _safe_reply(adapter, event, _ISSUE_READ_FAIL)
                audit.outcome = "get_issue_failed"
                return

            # ① Cancellation checkpoint after slow I/O — raises RunCancelledError
            # (BaseException, not Exception) if the user cancelled the run.
            # RunCancelledError propagates through audit_lifecycle's BaseException
            # guard, writing CANCELLED before re-raising to the worker.
            if run_id:
                await checkpoint(ctx.redis, run_id)

            default_branch = str(issue.get("default_branch", "main"))

            try:
                outcome = await _generate_fix_outcome(
                    sandbox=sandbox,
                    event=event,
                    adapter=adapter,
                    issue=issue,
                    run_id=run_id,
                    checkpointer=checkpointer,
                )
            except Exception:
                _logger.exception("fix_agent_failed", extra=_log_extra(event))
                await _safe_reply(adapter, event, _AGENT_FAIL)
                audit.outcome = "agent_failed"
                return

            # ② Checkpoint after the (potentially long) agent loop.
            if run_id:
                await checkpoint(ctx.redis, run_id)

            if not outcome.attempt.tests_passed:
                await _safe_reply(
                    adapter,
                    event,
                    _TESTS_FAILED_HEADER.format(
                        summary=outcome.attempt.summary,
                        cmd=outcome.attempt.test_command,
                        output=_truncate(outcome.attempt.test_output),
                    ),
                )
                audit.outcome = "tests_failed"
                return

            branch = _branch_name(issue_number=issue_number, base_sha=base_sha)

            try:
                await adapter.create_branch(event, branch, base_sha)
            except Exception:
                _logger.exception("fix_create_branch_failed", extra=_log_extra(event))
                await _safe_reply(adapter, event, _BRANCH_CONFLICT)
                audit.outcome = "create_branch_failed"
                return

            # ③ Checkpoint after branch creation.
            if run_id:
                await checkpoint(ctx.redis, run_id)

            # Refresh the installation token immediately before the push.
            # GitHub installation tokens have a ~1 h TTL; the fix-agent loop
            # can run 10-60+ minutes, so the token captured at dispatch time
            # (handle.token) may be expired by the time we push.  The adapter
            # caches tokens internally and refreshes them on demand, so this
            # call is cheap on a warm cache and correct on an expired one.
            try:
                fresh_token = await adapter.get_installation_token(event)
            except Exception:
                _logger.warning("fix_token_refresh_failed", extra=_log_extra(event))
                fresh_token = token  # fall back to the original token

            try:
                await sandbox.commit_and_push(
                    branch_ref=branch,
                    message=f"openbot: fix #{issue_number}",
                    token=fresh_token,
                )
            except Exception:
                _logger.exception("fix_push_failed", extra=_log_extra(event))
                await _safe_reply(adapter, event, _PUSH_FAIL)
                audit.outcome = "push_failed"
                return

            # ④ Checkpoint after push.
            if run_id:
                await checkpoint(ctx.redis, run_id)

            try:
                pr = await adapter.open_pull_request(
                    event,
                    title=_pr_title(str(issue.get("title", "")), issue_number),
                    body=_pr_body(
                        attempt_summary=outcome.attempt.summary,
                        issue_number=issue_number,
                        test_command=outcome.attempt.test_command,
                    ),
                    head=branch,
                    base=default_branch,
                )
            except Exception:
                _logger.exception("fix_open_pr_failed", extra=_log_extra(event))
                await _safe_reply(adapter, event, _OPEN_PR_FAIL)
                audit.outcome = "open_pr_failed"
                return

            pr_url = str(pr.get("html_url", ""))
            await _safe_reply(
                adapter,
                event,
                _PR_OPENED.format(
                    issue=issue_number,
                    url=pr_url,
                    summary=outcome.attempt.summary,
                    files=_files_changed_str(outcome.attempt.files_changed),
                    cmd=outcome.attempt.test_command,
                ),
            )
            audit.outcome = f"pr_opened:{pr_url}"
    finally:
        # ⑤ Cleanup: delete LangGraph checkpoint rows regardless of outcome
        # (success, agent failure, push failure, cancellation, etc.) so they
        # don't accumulate in Postgres indefinitely.
        if run_id and checkpointer is not None:
            try:
                await checkpointer.adelete_thread(run_id)
            except Exception:
                _logger.warning(
                    "fix_checkpoint_delete_failed",
                    extra={"run_id": run_id, **_log_extra(event)},
                )


async def _safe_reply(adapter: ChannelAdapterPort, event: UnifiedEvent, message: str) -> None:
    """Post a comment, swallowing any errors. The use case contract is
    "the fix workflow never raises out of pre-flight." Comment posting
    is best-effort UX — substantive work and audit are already recorded.
    """
    try:
        await adapter.reply(event, message)
    except Exception:
        _logger.exception("fix_reply_failed", extra=_log_extra(event))


def _log_extra(event: UnifiedEvent) -> dict[str, Any]:
    return {
        "delivery_id": event.delivery_id,
        "repo": event.repo,
        "issue_number": event.issue_number,
    }


__all__ = ["maybe_run_fix"]

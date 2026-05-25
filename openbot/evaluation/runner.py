"""openbot.evaluation.runner — eval entry points.

Each ``run_*_sample`` function:
  1. Builds a synthetic ``UnifiedEvent`` from the sample's metadata.
  2. Constructs an ``EvalChannelAdapter`` pre-loaded with the sample's
     diff / files / issue data.
  3. Calls the production responder (DeepAgentsReviewResponder, etc.)
     exactly as the use case does.
  4. Returns a typed eval result that scorers can inspect.

This means evals measure the production code path end-to-end, not a
parallel eval-only reimplementation.

Sandbox note:
  ``run_fix_sample`` accepts an optional ``sandbox`` argument.  When
  ``sandbox`` is ``None`` the function raises ``ValueError`` immediately
  (fix requires a working sandbox — evals should pass a real or fake one).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.evaluation.adapters import EvalChannelAdapter
from openbot.infrastructure.agents.deepagents_chat import DeepAgentsChatResponder
from openbot.infrastructure.agents.deepagents_fix import DeepAgentsFixResponder
from openbot.infrastructure.agents.deepagents_review import DeepAgentsReviewResponder

if TYPE_CHECKING:
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.domain.fix import FixOutcome
    from openbot.domain.review import ReviewFindings
    from openbot.evaluation.github_file_reader import GitHubFileReader


def _make_event(
    *,
    repo: str,
    actor: str,
    kind: EventKind,
    pr_number: int | None = None,
    issue_number: int | None = None,
    comment_body: str | None = None,
    delivery_id: str | None = None,
) -> UnifiedEvent:
    return UnifiedEvent(
        channel="eval",
        delivery_id=delivery_id or str(uuid.uuid4()),
        kind=kind,
        repo=repo,
        actor=actor,
        pr_number=pr_number,
        issue_number=issue_number,
        comment_body=comment_body,
    )


async def run_review_sample(
    *,
    repo: str,
    pr_number: int,
    pr_diff: str,
    actor: str = "eval-harness",
    files: dict[str, str] | None = None,
    file_reader: GitHubFileReader | None = None,
    run_id: str | None = None,
) -> ReviewFindings:
    """Run the production review responder on a synthetic PR.

    Args:
        repo:        ``"owner/name"`` — used in the LLM prompt context.
        pr_number:   The PR number (embedded in context; no real API call).
        pr_diff:     The full unified diff for the PR.
        actor:       Reviewer persona (defaults to "eval-harness").
        files:       Optional repo file map (priority cache for read_file / grep_repo).
        file_reader: Optional ``GitHubFileReader`` for live GitHub file access.
                     Cache misses in ``files`` fall through to this reader, keeping
                     file-fetching inside OpenBot's GitHub infrastructure layer.
        run_id:      Optional LangSmith run ID for tracing.

    Returns:
        ``ReviewFindings`` — the responder's structured output.
    """
    event = _make_event(
        repo=repo,
        actor=actor,
        kind=EventKind.PR_OPENED,
        pr_number=pr_number,
    )
    adapter = EvalChannelAdapter(pr_diff=pr_diff, files=files or {}, file_reader=file_reader)
    return await DeepAgentsReviewResponder().review_for_event(event, adapter=adapter, run_id=run_id)


async def run_fix_sample(
    *,
    repo: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    base_sha: str,
    clone_url: str,
    sandbox: SandboxPort,
    actor: str = "eval-harness",
    files: dict[str, str] | None = None,
    run_id: str | None = None,
) -> FixOutcome:
    """Run the production fix responder on a synthetic GitHub issue.

    Args:
        repo:         ``"owner/name"``.
        issue_number: The issue number.
        issue_title:  Issue title text.
        issue_body:   Issue body text (Markdown).
        base_sha:     The SHA the fix should branch from.
        clone_url:    HTTPS clone URL (used by the sandbox clone step).
        sandbox:      A live ``SandboxPort`` instance — required for fix.
        actor:        Assignee persona.
        files:        Optional repo file map.
        run_id:       Optional LangSmith run ID.

    Returns:
        ``FixOutcome`` — the responder's structured output.

    Raises:
        ValueError: if ``sandbox`` is ``None``.
    """
    event = _make_event(
        repo=repo,
        actor=actor,
        kind=EventKind.ISSUE_ASSIGNED,
        issue_number=issue_number,
    )
    adapter = EvalChannelAdapter(
        pr_diff="",  # fix loop doesn't read the PR diff
        files=files or {},
        issue={
            "title": issue_title,
            "body": issue_body,
            "comments": [],
            "base_sha": base_sha,
            "default_branch": "main",
            "clone_url": clone_url,
        },
    )
    return await DeepAgentsFixResponder().fix_for_event(
        event,
        adapter=adapter,
        sandbox=sandbox,
        issue={
            "title": issue_title,
            "body": issue_body,
            "base_sha": base_sha,
        },
        run_id=run_id,
    )


async def run_chat_sample(
    *,
    repo: str,
    user_request: str,
    actor: str = "eval-harness",
    issue_number: int | None = None,
    comment_body: str | None = None,
    run_id: str | None = None,
) -> str:
    """Run the production chat responder on a synthetic comment.

    Args:
        repo:         ``"owner/name"``.
        user_request: The human's message / question.
        actor:        Commenter persona.
        issue_number: Optional issue number for context.
        comment_body: Raw comment text (defaults to ``user_request``).
        run_id:       Optional LangSmith run ID.

    Returns:
        The bot's reply string.
    """
    event = _make_event(
        repo=repo,
        actor=actor,
        kind=EventKind.ISSUE_COMMENT_CREATED,
        issue_number=issue_number,
        comment_body=comment_body or user_request,
    )
    return await DeepAgentsChatResponder().reply_for_event(
        event, user_request=user_request, run_id=run_id
    )


async def run_test_generation_sample(
    *,
    repo: str,
    pr_number: int,
    pr_diff: str,
    actor: str = "eval-harness",
    files: dict[str, str] | None = None,
    run_id: str | None = None,
) -> Any:
    """Stub — test generation capability is not yet implemented in v0.1.

    Raises ``NotImplementedError`` so eval tasks that call this get a
    clear error rather than silently returning empty results.
    """
    raise NotImplementedError(
        "run_test_generation_sample: test generation is not yet implemented in v0.1. "
        "Use run_review_sample or run_fix_sample instead."
    )


__all__ = [
    "run_chat_sample",
    "run_fix_sample",
    "run_review_sample",
    "run_test_generation_sample",
]

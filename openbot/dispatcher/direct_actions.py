# openbot/dispatcher/direct_actions.py
"""Pure rule functions that evaluate EventContext and return a DirectAction.

Each function returns ``DirectAction`` if a canned reply should be sent
immediately (short-circuit, no enqueue) or ``None`` to let normal flow continue.

All functions are synchronous and side-effect-free.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from openbot.dispatcher.context import EventContext
from openbot.domain.workflows import Feature

__all__ = [
    "PR_OVERSIZED_THRESHOLD",
    "RULES_BY_FEATURE",
    "DirectAction",
    "check_issue_completeness",
    "check_mention_clarity",
    "check_pr_size",
]

PR_OVERSIZED_THRESHOLD: Final[int] = 500
"""Total lines changed (additions + deletions) above which a PR is flagged."""

_MENTION_MIN_CHARS: Final[int] = 20
"""Minimum characters in the stripped mention body to be considered substantive."""

# Canonical chat-prefix list lives in ``openbot.application.state.classifier``.
# Direct-action evaluation runs upstream of the state-machine classifier, so
# we re-declare here to avoid importing that module just for one constant.
_CHAT_PREFIXES: Final[tuple[str, ...]] = ("@openbot ", "@yibots ")


@dataclass(frozen=True, slots=True)
class DirectAction:
    """A canned reply to send without running the full LLM pipeline.

    ``drop`` is always True for v0.1 (return after reply, no enqueue).
    ``labels_to_add`` is empty unless the rule also wants to add a label.
    """

    message: str
    labels_to_add: tuple[str, ...] = ()
    drop: bool = True


def check_issue_completeness(ctx: EventContext) -> DirectAction | None:
    """Return a DirectAction when an issue body is empty or whitespace-only.

    Returns ``None`` when body key was absent or body has non-whitespace text.
    """
    if ctx.issue_body is None or ctx.issue_body.strip():
        return None
    return DirectAction(
        message=(
            "Thanks for opening this issue! 👋\n\n"
            "It looks like the description is empty. Could you add some details "
            "so we can help you better? For example:\n\n"
            "- What are you trying to do?\n"
            "- What did you expect to happen?\n"
            "- What actually happened?\n\n"
            "I've added the **needs-info** label in the meantime. "
            "Feel free to edit the issue body and I'll re-evaluate."
        ),
        labels_to_add=("needs-info",),
    )


def check_pr_size(ctx: EventContext) -> DirectAction | None:
    """Return a DirectAction when a PR changes more lines than the threshold."""
    total = ctx.pr_total_lines_changed
    if total <= PR_OVERSIZED_THRESHOLD:
        return None
    return DirectAction(
        message=(
            f"This PR changes **{total} lines** across {ctx.pr_changed_files} file(s), "
            f"which is above our review threshold of {PR_OVERSIZED_THRESHOLD} lines.\n\n"
            "Large PRs are harder to review thoroughly. Consider splitting this into "
            "smaller, focused PRs:\n\n"
            "- One PR per logical change or feature\n"
            "- Separate refactoring commits from behavior changes\n"
            "- Extract preparatory changes into a prerequisite PR\n\n"
            "If splitting is not practical, please add a note explaining why."
        ),
    )


def check_mention_clarity(ctx: EventContext) -> DirectAction | None:
    """Return a DirectAction when a @mention is too vague to act on.

    Returns ``None`` when there is no mention or the body is substantive.
    """
    if ctx.mention_body is None:
        return None
    body = ctx.mention_body.strip()
    if not body:
        return DirectAction(
            message=(
                "Hi! I'm OpenBot 👋 — you mentioned me but didn't include a request.\n\n"
                "Try something like:\n"
                "- `@openbot triage this issue`\n"
                "- `@openbot review the changes`\n"
                "- `@openbot help me fix this`"
            ),
        )
    for prefix in _CHAT_PREFIXES:
        if body.startswith(prefix):
            body = body[len(prefix) :].strip()
            break
    if len(body) < _MENTION_MIN_CHARS:
        return DirectAction(
            message=(
                "Thanks for reaching out! Your message was a bit short for me to act on.\n\n"
                "Could you describe what you need? For example:\n"
                "- `@openbot triage and label this issue`\n"
                "- `@openbot review my PR for correctness`"
            ),
        )
    return None


RULES_BY_FEATURE: Final[Mapping[Feature, Callable[[EventContext], DirectAction | None]]] = {
    Feature.TRIAGE: check_issue_completeness,
    Feature.REVIEW: check_pr_size,
    Feature.CHAT: check_mention_clarity,
}

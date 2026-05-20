"""Incremental PR review scope computation (pure, no I/O).

Determines whether a PR synchronize event can be reviewed incrementally
(last_reviewed_sha → head_sha) or needs a full re-review from the PR base.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DiffScope:
    """Resolved diff boundary for a PR review task.

    Attributes:
        base_sha: SHA to diff FROM (last_reviewed_sha for incremental; PR base for full).
        head_sha: SHA to diff TO (always the current PR head commit).
        is_incremental: True when only new commits since last review need checking.
        is_force_push: True when history was rewritten; full re-review required.
        last_reviewed_sha: The end-SHA from the previous review run, or None.
    """

    base_sha: str | None
    head_sha: str | None
    is_incremental: bool
    is_force_push: bool
    last_reviewed_sha: str | None


def compute_diff_scope(
    raw: dict[str, object],
    *,
    last_reviewed_sha: str | None,
) -> DiffScope:
    """Compute diff boundary from a raw PR webhook payload.

    Args:
        raw: ``event.raw`` dict from a PR opened/synchronize event.
        last_reviewed_sha: Commit SHA where the previous review run ended,
            or None for the first review.

    Returns:
        DiffScope with resolved boundaries and incremental/force-push flags.
    """
    pull_request = raw.get("pull_request") if isinstance(raw, dict) else None
    if not isinstance(pull_request, dict):
        return DiffScope(
            base_sha=None,
            head_sha=None,
            is_incremental=False,
            is_force_push=False,
            last_reviewed_sha=last_reviewed_sha,
        )

    head = pull_request.get("head")
    base = pull_request.get("base")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    base_sha = base.get("sha") if isinstance(base, dict) else None

    if last_reviewed_sha is None:
        return DiffScope(
            base_sha=base_sha,
            head_sha=head_sha,
            is_incremental=False,
            is_force_push=False,
            last_reviewed_sha=None,
        )

    before_sha = raw.get("before") if isinstance(raw, dict) else None

    if before_sha != last_reviewed_sha:
        # 'before' absent or mismatched → force push; history may be rewritten.
        return DiffScope(
            base_sha=base_sha,
            head_sha=head_sha,
            is_incremental=False,
            is_force_push=True,
            last_reviewed_sha=last_reviewed_sha,
        )

    # Normal incremental push: diff from last review end to new head.
    return DiffScope(
        base_sha=last_reviewed_sha,
        head_sha=head_sha,
        is_incremental=True,
        is_force_push=False,
        last_reviewed_sha=last_reviewed_sha,
    )

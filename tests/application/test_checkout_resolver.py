"""`resolve_checkout` — the ref-resolution matrix.

This is the single highest-risk surface in the unified sandbox slice
(spec § "The ref-resolution matrix"). One bug here = the agent reads
the wrong file at the wrong commit and gives the user a wrong answer.
Every cell in the matrix gets a unit test.

The resolver is pure-ish: no HTTP of its own, but it calls the
``ChannelAdapterPort`` for the two SHAs the webhook payload doesn't
carry (``default_branch`` HEAD for issue context, PR head/base SHAs
for issue-comment-on-PR). We stub the port with a minimal recording
double so the assertion target is *which adapter calls were made and
which CheckoutSpec came out*, not network shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from openbot.application.checkout_resolver import resolve_checkout
from openbot.domain.checkout import CheckoutResolutionError, CheckoutSpec, CloneStrategy
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Workflow

# Concrete SHAs used across tests — distinct values so a misrouting
# (returning base instead of head, etc.) is visible in the assertion.
_HEAD_SHA = "a" * 40
_BASE_SHA = "b" * 40
_DEFAULT_SHA = "c" * 40
_REVIEW_COMMIT_SHA = "d" * 40
_LAST_REVIEWED_SHA = "e" * 40
_CLONE_URL = "https://github.com/acme/widget.git"


@dataclass
class _StubAdapter:
    """Minimal `ChannelAdapterPort` double — only the methods the
    resolver actually calls (``get_default_branch_sha``,
    ``get_pull_request``). Records arguments so tests can assert the
    resolver issued exactly the expected adapter calls (no extras).
    """

    default_branch_sha: str = _DEFAULT_SHA
    pr_head_sha: str = _HEAD_SHA
    pr_base_sha: str = _BASE_SHA
    default_branch_calls: list[str | None] = field(default_factory=list)
    pr_calls: list[tuple[str | None, int]] = field(default_factory=list)

    async def get_default_branch_sha(self, event: UnifiedEvent) -> str:
        self.default_branch_calls.append(event.resource_key)
        return self.default_branch_sha

    async def get_pull_request(self, event: UnifiedEvent, pr_number: int) -> dict[str, Any]:
        self.pr_calls.append((event.resource_key, pr_number))
        return {
            "head": {"sha": self.pr_head_sha},
            "base": {"sha": self.pr_base_sha},
        }


def _event(
    *,
    kind: EventKind,
    pr_number: int | None = None,
    issue_number: int | None = None,
    review_commit_id: str | None = None,
    last_reviewed_sha: str | None = None,
    clone_url: str | None = _CLONE_URL,
) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="d-1",
        kind=kind,
        repo="acme/widget",
        actor="alice",
        pr_number=pr_number,
        issue_number=issue_number,
        clone_url=clone_url,
        review_commit_id=review_commit_id,
        last_reviewed_sha=last_reviewed_sha,
    )


# ─────────────────── Matrix row 1: ISSUE_OPENED → triage ───────────────────


@pytest.mark.asyncio
async def test_issue_opened_triage_uses_default_branch_sha() -> None:
    adapter = _StubAdapter()
    spec = await resolve_checkout(
        _event(kind=EventKind.ISSUE_OPENED, issue_number=42),
        Workflow.TRIAGE,
        adapter,
    )
    assert spec == CheckoutSpec(
        repo_url=_CLONE_URL,
        ref=_DEFAULT_SHA,
        strategy=CloneStrategy.SHALLOW,
        diff_base=None,
    )
    assert adapter.default_branch_calls == ["github:acme/widget:issue:42"]
    assert adapter.pr_calls == []  # no extra round-trip


# ─────────────────── Matrix row 2: ISSUE_ASSIGNED → fix ───────────────────


@pytest.mark.asyncio
async def test_issue_assigned_fix_uses_default_branch_sha() -> None:
    adapter = _StubAdapter()
    spec = await resolve_checkout(
        _event(kind=EventKind.ISSUE_ASSIGNED, issue_number=7),
        Workflow.FIX,
        adapter,
    )
    assert spec.ref == _DEFAULT_SHA
    assert spec.strategy is CloneStrategy.SHALLOW
    assert spec.diff_base is None


# ─────────────────── Matrix row 3: ISSUE_LABELED → fix ───────────────────


@pytest.mark.asyncio
async def test_issue_labeled_fix_uses_default_branch_sha() -> None:
    adapter = _StubAdapter()
    spec = await resolve_checkout(
        _event(kind=EventKind.ISSUE_LABELED, issue_number=9),
        Workflow.FIX,
        adapter,
    )
    assert spec.ref == _DEFAULT_SHA
    assert spec.strategy is CloneStrategy.SHALLOW


# ─────────────── Matrix row 4: ISSUE_COMMENT_CREATED on issue → chat ──────


@pytest.mark.asyncio
async def test_issue_comment_on_issue_chat_uses_default_branch_blobless() -> None:
    adapter = _StubAdapter()
    spec = await resolve_checkout(
        _event(kind=EventKind.ISSUE_COMMENT_CREATED, issue_number=3),
        Workflow.CHAT,
        adapter,
    )
    assert spec.ref == _DEFAULT_SHA
    assert spec.strategy is CloneStrategy.BLOBLESS
    assert spec.diff_base is None


# ─────────────── Matrix row 5: ISSUE_COMMENT_CREATED on PR → chat ──────────


@pytest.mark.asyncio
async def test_issue_comment_on_pr_chat_uses_pr_head_sha_blobless() -> None:
    """Issue-comment payloads on a PR don't carry the head SHA — the
    resolver must round-trip to GitHub via ``get_pull_request``.
    """
    adapter = _StubAdapter()
    spec = await resolve_checkout(
        _event(kind=EventKind.ISSUE_COMMENT_CREATED, pr_number=17),
        Workflow.CHAT,
        adapter,
    )
    assert spec.ref == _HEAD_SHA
    assert spec.strategy is CloneStrategy.BLOBLESS
    assert spec.diff_base is None
    assert adapter.pr_calls == [("github:acme/widget:pr:17", 17)]


# ─────────────────── Matrix row 6: PR_OPENED → review ───────────────────


@pytest.mark.asyncio
async def test_pr_opened_review_uses_head_with_base_as_diff_base() -> None:
    adapter = _StubAdapter()
    spec = await resolve_checkout(
        _event(kind=EventKind.PR_OPENED, pr_number=17),
        Workflow.REVIEW,
        adapter,
    )
    assert spec == CheckoutSpec(
        repo_url=_CLONE_URL,
        ref=_HEAD_SHA,
        strategy=CloneStrategy.SHALLOW_HISTORY,
        diff_base=_BASE_SHA,
    )


# ───────────── Matrix row 7: PR_SYNCHRONIZED → review (incremental) ────────


@pytest.mark.asyncio
async def test_pr_synchronized_review_prefers_last_reviewed_sha_as_diff_base() -> None:
    """Incremental review: diff against the *last* SHA we reviewed,
    not the PR's base — otherwise we re-review commits we already did.
    """
    adapter = _StubAdapter()
    spec = await resolve_checkout(
        _event(
            kind=EventKind.PR_SYNCHRONIZED,
            pr_number=17,
            last_reviewed_sha=_LAST_REVIEWED_SHA,
        ),
        Workflow.REVIEW,
        adapter,
    )
    assert spec.ref == _HEAD_SHA
    assert spec.diff_base == _LAST_REVIEWED_SHA
    assert spec.strategy is CloneStrategy.SHALLOW_HISTORY


@pytest.mark.asyncio
async def test_pr_synchronized_review_falls_back_to_base_sha_when_no_prior_review() -> None:
    adapter = _StubAdapter()
    spec = await resolve_checkout(
        _event(kind=EventKind.PR_SYNCHRONIZED, pr_number=17),
        Workflow.REVIEW,
        adapter,
    )
    assert spec.diff_base == _BASE_SHA


# ─────────────────── Matrix row 8: PR_REOPENED → review ───────────────────


@pytest.mark.asyncio
async def test_pr_reopened_review_is_fresh_review_against_base() -> None:
    adapter = _StubAdapter()
    spec = await resolve_checkout(
        _event(kind=EventKind.PR_REOPENED, pr_number=17),
        Workflow.REVIEW,
        adapter,
    )
    assert spec.ref == _HEAD_SHA
    assert spec.diff_base == _BASE_SHA
    assert spec.strategy is CloneStrategy.SHALLOW_HISTORY


# ─────── Matrix row 9: PR_REVIEW_COMMENT_CREATED (inline) → chat ──────────


@pytest.mark.asyncio
async def test_pr_review_comment_uses_commit_id_blobless() -> None:
    """Inline review comments must check out the *exact* commit they
    were left on — line numbers drift if the file changed in a later
    commit. We use ``review_commit_id`` from the webhook directly and
    skip the ``get_pull_request`` round-trip entirely.
    """
    adapter = _StubAdapter()
    spec = await resolve_checkout(
        _event(
            kind=EventKind.PR_REVIEW_COMMENT_CREATED,
            pr_number=17,
            review_commit_id=_REVIEW_COMMIT_SHA,
        ),
        Workflow.CHAT,
        adapter,
    )
    assert spec.ref == _REVIEW_COMMIT_SHA
    assert spec.strategy is CloneStrategy.BLOBLESS
    assert spec.diff_base is None
    # No adapter call — we already had the SHA.
    assert adapter.pr_calls == []
    assert adapter.default_branch_calls == []


@pytest.mark.asyncio
async def test_pr_review_comment_without_commit_id_falls_back_to_pr_head() -> None:
    """Defensive: if a malformed payload omits ``commit_id``, fall
    into the generic PR-context branch instead of crashing.
    """
    adapter = _StubAdapter()
    spec = await resolve_checkout(
        _event(kind=EventKind.PR_REVIEW_COMMENT_CREATED, pr_number=17),
        Workflow.CHAT,
        adapter,
    )
    assert spec.ref == _HEAD_SHA
    assert adapter.pr_calls == [("github:acme/widget:pr:17", 17)]


# ─────────────────── Edge cases ───────────────────


@pytest.mark.asyncio
async def test_missing_clone_url_raises_resolution_error() -> None:
    adapter = _StubAdapter()
    with pytest.raises(CheckoutResolutionError, match="clone_url"):
        await resolve_checkout(
            _event(kind=EventKind.ISSUE_OPENED, issue_number=42, clone_url=None),
            Workflow.TRIAGE,
            adapter,
        )


@pytest.mark.asyncio
async def test_unknown_event_without_context_raises_resolution_error() -> None:
    """``EventKind.UNKNOWN`` with no PR/issue number can't be
    resolved — the router should have filtered this out via
    ``SandboxPolicy.NO_SANDBOX``, but the resolver enforces the
    contract defensively.
    """
    adapter = _StubAdapter()
    with pytest.raises(CheckoutResolutionError, match="no resolution rule"):
        await resolve_checkout(
            _event(kind=EventKind.UNKNOWN),
            Workflow.CHAT,
            adapter,
        )


# ─────────────────── Property tests (parametrize over the matrix) ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "workflow"),
    [
        (EventKind.PR_OPENED, Workflow.REVIEW),
        (EventKind.PR_SYNCHRONIZED, Workflow.REVIEW),
        (EventKind.PR_REOPENED, Workflow.REVIEW),
        (EventKind.ISSUE_COMMENT_CREATED, Workflow.CHAT),  # comment on PR
    ],
)
async def test_pr_context_events_resolve_to_head_sha(kind: EventKind, workflow: Workflow) -> None:
    """Every event with ``pr_number is not None`` (and no inline
    ``review_commit_id``) resolves to the PR head SHA.
    """
    adapter = _StubAdapter()
    spec = await resolve_checkout(
        _event(kind=kind, pr_number=17),
        workflow,
        adapter,
    )
    assert spec.ref == _HEAD_SHA


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "workflow"),
    [
        (EventKind.ISSUE_OPENED, Workflow.TRIAGE),
        (EventKind.ISSUE_ASSIGNED, Workflow.FIX),
        (EventKind.ISSUE_LABELED, Workflow.FIX),
        (EventKind.ISSUE_COMMENT_CREATED, Workflow.CHAT),  # comment on issue
    ],
)
async def test_issue_context_events_resolve_to_default_branch_sha(
    kind: EventKind, workflow: Workflow
) -> None:
    """Every event with ``issue_number is not None`` (and no
    ``pr_number``) resolves to the default branch HEAD.
    """
    adapter = _StubAdapter()
    spec = await resolve_checkout(
        _event(kind=kind, issue_number=42),
        workflow,
        adapter,
    )
    assert spec.ref == _DEFAULT_SHA


@pytest.mark.asyncio
async def test_diff_base_only_set_for_review_workflow() -> None:
    """`diff_base` is non-None iff workflow is REVIEW *and* event has
    a `pr_number` — every other combination leaves it None.
    """
    adapter = _StubAdapter()

    # REVIEW + PR → diff_base set
    spec_review = await resolve_checkout(
        _event(kind=EventKind.PR_OPENED, pr_number=17),
        Workflow.REVIEW,
        adapter,
    )
    assert spec_review.diff_base is not None

    # CHAT on PR → diff_base None
    spec_chat = await resolve_checkout(
        _event(kind=EventKind.ISSUE_COMMENT_CREATED, pr_number=17),
        Workflow.CHAT,
        adapter,
    )
    assert spec_chat.diff_base is None

    # TRIAGE on issue → diff_base None
    spec_triage = await resolve_checkout(
        _event(kind=EventKind.ISSUE_OPENED, issue_number=42),
        Workflow.TRIAGE,
        adapter,
    )
    assert spec_triage.diff_base is None

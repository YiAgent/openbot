"""Tests for ``maybe_run_review`` — severity filter + PR Review API submission."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openbot.application.middleware import PreflightContext
from openbot.application.router import Dispatch, derive_task_id
from openbot.application.use_cases.review import maybe_run_review
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.review import Finding, ReviewFindings
from openbot.domain.workflows import Feature
from openbot.infrastructure.config_loader import baked_in_defaults


def _event(*, pr_number: int = 42) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="rev-deliv-1",
        kind=EventKind.PR_OPENED,
        repo="YiAgent/openbot",
        actor="alice",
        pr_number=pr_number,
        installation_id=100,
    )


def _adapter() -> Any:
    adapter = AsyncMock()
    adapter.create_pr_review = AsyncMock(return_value={"id": 999, "state": "COMMENTED"})
    return adapter


def _ctx(adapter: Any, event: UnifiedEvent, **config_overrides: Any) -> PreflightContext:
    config = baked_in_defaults()
    if config_overrides:
        config = replace(config, **config_overrides)
    return PreflightContext(
        event=event,
        dispatch=Dispatch(Feature.REVIEW, maybe_run_review, derive_task_id(event)),
        config=config,
        adapter=adapter,
        session_factory=None,
        redis=None,
    )


def _patch_findings(monkeypatch: pytest.MonkeyPatch, findings: ReviewFindings | Exception) -> None:
    async def _fake(
        *,
        event: UnifiedEvent,
        adapter: Any,
        run_id: str | None = None,
        checkpointer: Any = None,
    ) -> ReviewFindings:
        if isinstance(findings, Exception):
            raise findings
        return findings

    import openbot.application.use_cases.review as mod

    monkeypatch.setattr(mod, "_generate_review_findings", _fake)


# ───── zero findings → APPROVE ─────


async def test_no_findings_submits_approve_review(monkeypatch) -> None:
    adapter = _adapter()
    _patch_findings(monkeypatch, ReviewFindings(summary="LGTM"))

    await maybe_run_review(_ctx(adapter, _event()))

    adapter.create_pr_review.assert_awaited_once()
    kwargs = adapter.create_pr_review.await_args.kwargs
    args = adapter.create_pr_review.await_args.args
    # Signature: create_pr_review(event, pr_number, *, body, event_type, comments)
    assert args[1] == 42  # pr_number
    assert kwargs["event_type"] == "APPROVE"
    assert kwargs["body"] == "LGTM"
    assert kwargs["comments"] is None


# ───── above-threshold findings → COMMENT review with inline comments ─────


async def test_inline_findings_become_pr_review_comments(monkeypatch) -> None:
    adapter = _adapter()
    _patch_findings(
        monkeypatch,
        ReviewFindings(
            summary="2 issues",
            findings=(
                Finding(severity="high", file="src/a.py", message="null deref", line=10),
                Finding(severity="medium", file="src/b.py", message="race", line=22, quote="x = 1"),
            ),
        ),
    )

    await maybe_run_review(_ctx(adapter, _event()))

    kwargs = adapter.create_pr_review.await_args.kwargs
    assert kwargs["event_type"] == "COMMENT"
    assert kwargs["body"] == "2 issues"  # no repo-wide section
    comments = kwargs["comments"]
    assert len(comments) == 2
    assert comments[0] == {
        "path": "src/a.py",
        "line": 10,
        "body": "**high** — null deref",
    }
    assert comments[1]["path"] == "src/b.py"
    assert comments[1]["line"] == 22
    assert "**medium**" in comments[1]["body"]
    assert "x = 1" in comments[1]["body"]  # quote is rendered


# ───── severity threshold filters below-threshold findings ─────


async def test_below_threshold_findings_drop_to_approve(monkeypatch) -> None:
    """All findings are 'nit' or 'low' — threshold=medium drops everything → APPROVE."""
    adapter = _adapter()
    _patch_findings(
        monkeypatch,
        ReviewFindings(
            summary="Style nits only",
            findings=(
                Finding(severity="nit", file="x.py", message="style", line=1),
                Finding(severity="low", file="y.py", message="tiny", line=2),
            ),
        ),
    )

    await maybe_run_review(_ctx(adapter, _event()))  # default threshold = medium

    kwargs = adapter.create_pr_review.await_args.kwargs
    assert kwargs["event_type"] == "APPROVE"
    assert kwargs["comments"] is None


async def test_threshold_keeps_mixed_severities(monkeypatch) -> None:
    """threshold=medium keeps critical/high/medium, drops low/nit."""
    adapter = _adapter()
    _patch_findings(
        monkeypatch,
        ReviewFindings(
            summary="3 findings",
            findings=(
                Finding(severity="critical", file="a.py", message="boom", line=1),
                Finding(severity="nit", file="b.py", message="style", line=2),
                Finding(severity="medium", file="c.py", message="meh", line=3),
            ),
        ),
    )

    await maybe_run_review(_ctx(adapter, _event()))

    kwargs = adapter.create_pr_review.await_args.kwargs
    assert kwargs["event_type"] == "COMMENT"
    paths = [c["path"] for c in kwargs["comments"]]
    assert "a.py" in paths
    assert "c.py" in paths
    assert "b.py" not in paths  # nit dropped


# ───── repo-wide findings (no line) → folded into body ─────


async def test_repo_wide_findings_fold_into_body(monkeypatch) -> None:
    adapter = _adapter()
    _patch_findings(
        monkeypatch,
        ReviewFindings(
            summary="1 inline + 1 repo-wide",
            findings=(
                Finding(severity="high", file="a.py", message="inline", line=5),
                # No `line` → repo-wide; GitHub PR Review API can't render this inline.
                Finding(severity="high", file="CHANGELOG.md", message="entry missing"),
            ),
        ),
    )

    await maybe_run_review(_ctx(adapter, _event()))

    kwargs = adapter.create_pr_review.await_args.kwargs
    # Only the inline finding becomes a PR review comment.
    assert len(kwargs["comments"]) == 1
    assert kwargs["comments"][0]["path"] == "a.py"
    # The repo-wide finding lives in the body, not in the comments array.
    body = kwargs["body"]
    assert "1 inline + 1 repo-wide" in body
    assert "Repo-wide notes" in body
    assert "CHANGELOG.md" in body
    assert "entry missing" in body
    # Still COMMENT event since at least one finding passed the threshold.
    assert kwargs["event_type"] == "COMMENT"


# ───── responder failure → fallback COMMENT review ─────


async def test_responder_failure_posts_error_template(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = _adapter()
    _patch_findings(monkeypatch, RuntimeError("agent boom"))

    with caplog.at_level(logging.ERROR, logger="openbot.application.use_cases.review"):
        await maybe_run_review(_ctx(adapter, _event()))

    kwargs = adapter.create_pr_review.await_args.kwargs
    # Failure path posts COMMENT (not APPROVE) so the PR doesn't get a green
    # check based on a broken reviewer.
    assert kwargs["event_type"] == "COMMENT"
    assert "couldn't complete the review" in kwargs["body"]
    assert kwargs["comments"] is None
    assert any(r.message == "review_agent_reply_failed" for r in caplog.records)


# ───── skip when no pr_number / installation_id ─────


async def test_skips_when_pr_number_missing(monkeypatch) -> None:
    adapter = _adapter()
    _patch_findings(monkeypatch, ReviewFindings(summary="x"))

    bad_event = UnifiedEvent(
        channel="github",
        delivery_id="d",
        kind=EventKind.PR_OPENED,
        repo="acme/x",
        actor="a",
        pr_number=None,
        installation_id=100,
    )

    await maybe_run_review(_ctx(adapter, bad_event))

    adapter.create_pr_review.assert_not_awaited()


async def test_skips_when_installation_id_missing(monkeypatch) -> None:
    adapter = _adapter()
    _patch_findings(monkeypatch, ReviewFindings(summary="x"))

    bad_event = UnifiedEvent(
        channel="github",
        delivery_id="d",
        kind=EventKind.PR_OPENED,
        repo="acme/x",
        actor="a",
        pr_number=42,
        installation_id=None,
    )

    await maybe_run_review(_ctx(adapter, bad_event))

    adapter.create_pr_review.assert_not_awaited()


# ───── never emits REQUEST_CHANGES ─────


async def test_never_emits_request_changes_even_on_critical(monkeypatch) -> None:
    """PRD §13 lock: advisory only — never block merge."""
    adapter = _adapter()
    _patch_findings(
        monkeypatch,
        ReviewFindings(
            summary="critical bug",
            findings=(Finding(severity="critical", file="x.py", message="rce", line=1),),
        ),
    )

    await maybe_run_review(_ctx(adapter, _event()))

    assert adapter.create_pr_review.await_args.kwargs["event_type"] == "COMMENT"


# ───── run_id + checkpointer forwarding ─────


def _make_preflight_ctx(
    event: UnifiedEvent,
    adapter: Any,
    agent_checkpointer: Any = None,
    **config_overrides: Any,
) -> PreflightContext:
    """Like ``_ctx`` but also accepts ``agent_checkpointer``."""
    config = baked_in_defaults()
    if config_overrides:
        config = replace(config, **config_overrides)
    return PreflightContext(
        event=event,
        dispatch=Dispatch(Feature.REVIEW, maybe_run_review, derive_task_id(event)),
        config=config,
        adapter=adapter,
        session_factory=None,
        redis=None,
        agent_checkpointer=agent_checkpointer,
    )


async def test_review_use_case_passes_run_id_and_checkpointer_to_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """maybe_run_review must forward ctx.dispatch.run_id and ctx.agent_checkpointer."""
    from langgraph.checkpoint.memory import MemorySaver

    import openbot.application.use_cases.review as review_mod
    from openbot.domain.review import ReviewFindings

    captured: dict[str, Any] = {}

    async def fake_generate(
        *,
        event: Any,
        adapter: Any,
        run_id: str | None = None,
        checkpointer: Any = None,
    ) -> ReviewFindings:
        captured["run_id"] = run_id
        captured["checkpointer"] = checkpointer
        return ReviewFindings(summary="ok", findings=())

    monkeypatch.setattr(review_mod, "_generate_review_findings", fake_generate)

    saver = MemorySaver()
    event = _event()
    adapter = _adapter()
    ctx = _make_preflight_ctx(event=event, adapter=adapter, agent_checkpointer=saver)

    await review_mod.maybe_run_review(ctx)

    assert captured.get("run_id") == ctx.dispatch.run_id
    assert captured.get("checkpointer") is saver

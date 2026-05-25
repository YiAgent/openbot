"""Comment renderers for the reproduce loop — pure-function snapshot tests.

Renderers MUST stay pure (no I/O, no ``await``, no logging) so this file
can pin every visible byte. The four status templates double as the
public-facing API contract of the reproduce flow — a regression here is
a user-visible regression.

Security note: ``output_excerpt`` and ``hypothesis`` are emitted by the
LLM, so they are untrusted strings from the renderer's perspective. The
``test_xss_*`` cases pin the fencing + ``html.escape`` boundary.
"""

from __future__ import annotations

import pytest

from openbot.domain.repro import ReproOutcome, ReproStatus

# ── Fixture helpers ─────────────────────────────────────────────────────────


def _outcome(
    status: ReproStatus = ReproStatus.REPRODUCED,
    *,
    summary: str = "IndexError reproduced on empty list",
    command: str | None = "pytest -q tests/test_list.py",
    exit_code: int | None = 1,
    output_excerpt: str = "IndexError: list index out of range",
    hypothesis: str = "pop() called without length guard at api/list.py:42",
    repro_artifact: str | None = "#!/usr/bin/env bash\npytest -q tests/test_list.py\n",
    repro_artifact_filename: str | None = "repro_reproduced.sh",
) -> ReproOutcome:
    """Build a ReproOutcome with sensible defaults per-status.

    Defaults are the REPRODUCED shape; tests that need other statuses
    override only the fields that legitimately change so the assertion
    is honest about which value the template is reading.
    """

    return ReproOutcome(
        status=status,
        summary=summary,
        command=command,
        exit_code=exit_code,
        output_excerpt=output_excerpt,
        hypothesis=hypothesis,
        repro_artifact=repro_artifact,
        repro_artifact_filename=repro_artifact_filename,
    )


# ── render_thinking_comment ─────────────────────────────────────────────────


def test_thinking_comment_contains_robot_and_actor() -> None:
    """Spec §6.2: ``render_thinking_comment(actor)`` must mention the
    user (``@actor``) so the placeholder visibly belongs to *their*
    issue, and must use ``:robot:`` so style stays consistent with the
    other OpenBot replies."""
    from openbot.application.use_cases._repro_render import render_thinking_comment

    body = render_thinking_comment(actor="alice")
    assert body, "thinking comment must not be empty"
    assert ":robot:" in body
    assert "@alice" in body


def test_thinking_comment_handles_none_actor_safely() -> None:
    """``event.actor`` is ``str | None`` per UnifiedEvent — the renderer
    must not interpolate the literal string ``None`` into the comment."""
    from openbot.application.use_cases._repro_render import render_thinking_comment

    body = render_thinking_comment(actor=None)
    assert "None" not in body
    # Convention shared with triage's ACK template: fall back to "there".
    assert "@there" in body or "@maintainer" in body


# ── render_final_comment(None, ack_only=True) — preserves current ACK ───────


def test_ack_only_matches_current_triage_template() -> None:
    """Spec §R3.1: ``render_final_comment(None, ack_only=True)`` must
    equal the existing ``triage.py:_ACK_TEMPLATE.format(actor=...)`` so
    the rendered ACK doesn't change when triage.py switches over.

    Snapshotting against the *source* template (not a hand-typed copy)
    means a future edit to ``_ACK_TEMPLATE`` will fail this test loudly
    — which is the point. Both sites must update together.
    """
    from openbot.application.use_cases._repro_render import render_final_comment
    from openbot.application.use_cases.triage import _ACK_TEMPLATE

    expected = _ACK_TEMPLATE.format(actor="alice")
    assert render_final_comment(None, actor="alice", ack_only=True) == expected


def test_ack_only_handles_none_actor() -> None:
    from openbot.application.use_cases._repro_render import render_final_comment
    from openbot.application.use_cases.triage import _ACK_TEMPLATE

    expected = _ACK_TEMPLATE.format(actor="there")
    assert render_final_comment(None, actor=None, ack_only=True) == expected


# ── REPRODUCED template ─────────────────────────────────────────────────────


def test_reproduced_template_contains_required_elements() -> None:
    """REPRODUCED template must include:
    - the success marker ``:white_check_mark:``,
    - the failing ``command``,
    - the ``repro_artifact`` inside a ``<details>`` block with a fence,
    - the ``output_excerpt`` (escaped),
    - the ``hypothesis``."""
    from openbot.application.use_cases._repro_render import render_final_comment

    out = _outcome()
    body = render_final_comment(out, actor="alice")

    assert ":white_check_mark:" in body
    assert "pytest -q tests/test_list.py" in body
    assert "<details>" in body
    assert "IndexError: list index out of range" in body
    assert "pop() called without length guard at api/list.py:42" in body
    # The artifact body is fenced inside the details block.
    assert "#!/usr/bin/env bash" in body


def test_reproduced_template_includes_repro_artifact_filename() -> None:
    """Maintainers need to know what filename to drop the script into."""
    from openbot.application.use_cases._repro_render import render_final_comment

    body = render_final_comment(_outcome(), actor="alice")
    assert "repro_reproduced.sh" in body


# ── NOT_REPRODUCED template ────────────────────────────────────────────────


def test_not_reproduced_template_contains_required_elements() -> None:
    """NOT_REPRODUCED template must include:
    - the warning marker ``:warning:``,
    - the command the agent tried,
    - the ``exit_code`` so the maintainer can re-judge (0 vs non-zero
      may mean the bug was already fixed)."""
    from openbot.application.use_cases._repro_render import render_final_comment

    out = _outcome(
        status=ReproStatus.NOT_REPRODUCED,
        summary="ran the command, no failure observed",
        exit_code=0,
        output_excerpt="All tests passed (1 in 0.3s)",
        repro_artifact=None,
        repro_artifact_filename=None,
    )
    body = render_final_comment(out, actor="alice")

    assert ":warning:" in body
    assert "pytest -q tests/test_list.py" in body
    assert "exit code" in body.lower() or "exit_code" in body or "exited 0" in body
    # No details/artifact block for the not-reproduced case — there's
    # no script to share.
    assert "repro_reproduced.sh" not in body


# ── INSUFFICIENT_INFO template ──────────────────────────────────────────────


def test_insufficient_info_template_contains_required_elements() -> None:
    """INSUFFICIENT_INFO template must include:
    - the ``:speech_balloon:`` marker (conversation, not error),
    - the missing-info ``hypothesis`` rendered as a list so the user
      can answer one item at a time.

    The agent populates ``hypothesis`` with newline-delimited bullet
    points; the renderer is responsible for turning them into Markdown
    list rows so they actually render."""
    from openbot.application.use_cases._repro_render import render_final_comment

    out = _outcome(
        status=ReproStatus.INSUFFICIENT_INFO,
        summary="Could not reproduce — issue is missing key details",
        command=None,
        exit_code=None,
        output_excerpt="",
        hypothesis="no failing command\nno expected output\nno stack trace",
        repro_artifact=None,
        repro_artifact_filename=None,
    )
    body = render_final_comment(out, actor="alice")

    assert ":speech_balloon:" in body
    # Each missing-info line should be a Markdown list item.
    assert "- no failing command" in body
    assert "- no expected output" in body
    assert "- no stack trace" in body


# ── AGENT_FAILED template ──────────────────────────────────────────────────


def test_agent_failed_template_uses_spec_string() -> None:
    """Spec §5.3 pins the user-visible string verbatim. The exception
    class name MUST NOT appear in the comment — it would leak internals
    (e.g. ``AgentTimeoutError``) to an external user."""
    from openbot.application.use_cases._repro_render import render_final_comment

    out = _outcome(
        status=ReproStatus.AGENT_FAILED,
        summary="internal error — see audit log",
        command=None,
        exit_code=None,
        output_excerpt="",
        hypothesis="AgentTimeoutError: wall_seconds=180 exceeded",
        repro_artifact=None,
        repro_artifact_filename=None,
    )
    body = render_final_comment(out, actor="alice")

    assert ":robot:" in body
    assert "encountered an error" in body
    assert "re-trigger" in body.lower() or "re-open" in body.lower()
    # Spec §5.3: no exception class name in the rendered comment.
    assert "AgentTimeoutError" not in body
    # Defence-in-depth: also pin the audit-only fields don't leak.
    assert "wall_seconds" not in body


def test_agent_failed_does_not_leak_exception_class_via_summary() -> None:
    """The summary field is also LLM-produced; even if some upstream
    code stuffs an exception class name into it, the rendered comment
    must stay generic. The template should NOT interpolate ``summary``
    for the AGENT_FAILED status — the spec §5.3 string is fully static.
    """
    from openbot.application.use_cases._repro_render import render_final_comment

    out = _outcome(
        status=ReproStatus.AGENT_FAILED,
        # An attacker (or sloppy upstream) might try to inject details via summary.
        summary="AgentBudgetExhaustedError: model_call_limit=15 exceeded",
        command=None,
        exit_code=None,
        output_excerpt="",
        hypothesis="",
        repro_artifact=None,
        repro_artifact_filename=None,
    )
    body = render_final_comment(out, actor="alice")
    assert "AgentBudgetExhaustedError" not in body
    assert "model_call_limit" not in body


# ── Length / truncation budget ──────────────────────────────────────────────


def test_long_output_excerpt_rendered_with_truncation_marker() -> None:
    """The schema truncates ``output_excerpt`` at 2000 chars, but a
    runaway agent could in theory bypass the boundary. The renderer
    must enforce a hard ceiling so the rendered comment never blows
    past the spec §R3.1 length budget (≤ 4000 chars overall)."""
    from openbot.application.use_cases._repro_render import render_final_comment

    long_excerpt = "x" * 10_000
    out = _outcome(output_excerpt=long_excerpt)
    body = render_final_comment(out, actor="alice")
    # Total rendered length stays under 4000 chars — GitHub comment-size
    # friendly and predictable for the sticky update path.
    assert len(body) <= 4000
    # And the truncation marker is visible so the user knows the comment
    # is not a complete dump.
    assert "[truncated]" in body or "…" in body


# ── XSS / fence-break defence ───────────────────────────────────────────────


def test_xss_payload_in_output_excerpt_is_neutralised() -> None:
    """Spec §R3.1: outcome with ``output_excerpt="</details><script>...``
    must not produce raw ``<script>`` or an unescaped ``</details>``
    inside the rendered fenced block.

    Either fenced-codeblock-broken OR HTML-escaped is acceptable —
    we assert both: that ``<script`` and unescaped ``</details>`` do not
    appear inside the body. GitHub Markdown is the actual sanitisation
    layer, but defence-in-depth means we escape *before* interpolation
    too so a future move to non-GitHub channels stays safe.
    """
    from openbot.application.use_cases._repro_render import render_final_comment

    out = _outcome(
        output_excerpt="</details><script>alert('xss')</script>",
        hypothesis="</details><script>alert('xss')</script>",
    )
    body = render_final_comment(out, actor="alice")
    # No raw script tag — the LLM string was either escaped or rendered
    # inside a code fence so GitHub will not execute it.
    assert "<script>" not in body
    assert "<script " not in body
    # No raw </details> close-tag either — that would let an attacker
    # break out of our wrapper and render arbitrary HTML against us.
    assert "</details>" not in body or "&lt;/details&gt;" in body


def test_xss_payload_in_summary_is_neutralised() -> None:
    """``summary`` is also LLM-produced; the same escaping applies."""
    from openbot.application.use_cases._repro_render import render_final_comment

    out = _outcome(summary="<script>alert(1)</script>")
    body = render_final_comment(out, actor="alice")
    assert "<script>" not in body


def test_xss_payload_in_repro_artifact_is_neutralised() -> None:
    """The repro artifact is inside a fenced code block, so backticks
    inside it are the real threat (they could close the fence). At
    minimum, the rendered output must not produce an executable
    ``<script>`` tag outside a code fence."""
    from openbot.application.use_cases._repro_render import render_final_comment

    out = _outcome(repro_artifact="<script>alert(1)</script>\n```\n")
    body = render_final_comment(out, actor="alice")
    # Either fenced (so backticks/script tags are inert) or escaped.
    assert "<script>" not in body


# ── Renderers are pure (no side effects) ────────────────────────────────────


def test_renderers_are_idempotent() -> None:
    """Pure function discipline — same inputs must produce byte-identical
    output across calls. If a renderer captures wall-clock time / random
    nonces, this catches it."""
    from openbot.application.use_cases._repro_render import (
        render_final_comment,
        render_thinking_comment,
    )

    out = _outcome()
    assert render_thinking_comment(actor="alice") == render_thinking_comment(actor="alice")
    assert render_final_comment(out, actor="alice") == render_final_comment(out, actor="alice")
    # And the ack_only path too.
    assert render_final_comment(None, actor="alice", ack_only=True) == render_final_comment(
        None, actor="alice", ack_only=True
    )


# ── Static type / API surface ───────────────────────────────────────────────


def test_render_final_comment_requires_outcome_when_not_ack_only() -> None:
    """Calling ``render_final_comment(None, ack_only=False)`` is a
    programming error — the use case must pass either ``outcome`` or
    set ``ack_only=True``. Failing loudly here is better than silently
    rendering an empty body."""
    from openbot.application.use_cases._repro_render import render_final_comment

    with pytest.raises((TypeError, ValueError, AssertionError)):
        render_final_comment(None, actor="alice")  # ack_only defaults to False

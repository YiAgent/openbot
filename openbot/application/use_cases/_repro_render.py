"""Pure-function comment renderers for the reproduce loop — slice R3.

Why pure / no I/O:

  - Sticky-reply uses these strings as immediate placeholder bodies and
    later as the PATCH body for the same comment. The renderer must be
    a value transform from ``(outcome, actor)`` to ``str`` so the use
    case can pass the result around without re-running the agent.
  - Snapshot tests can pin every visible byte. The fenced output is the
    contract — see ``tests/application/use_cases/test_repro_render.py``.

Security model:

  - ``output_excerpt``, ``hypothesis``, ``summary``, ``command``, and
    ``repro_artifact`` are LLM-produced strings — treat as untrusted.
  - ``html.escape`` (stdlib, well-tested) is applied to every untrusted
    value *before* interpolation. We deliberately do NOT depend on
    Bleach/Ammonia here — those are for sanitizing user-submitted HTML;
    our problem is the inverse (we emit Markdown and only need entity
    escaping for the few values that could break our wrapper).
  - The triple-backtick code fences and ``<details>`` block are GitHub's
    actual sanitisation boundary; the in-process escaping is
    defence-in-depth so the same renderer is safe if we ever ship it to
    a non-GitHub channel.

Length budget:

  - The renderer enforces a hard ≤ 4000 char ceiling on the final
    string (see ``_OUTPUT_LENGTH_BUDGET``). The pydantic schema already
    truncates ``output_excerpt`` at 2000 chars, but a runaway agent
    *could* stuff multi-KB content into other fields. The final clamp
    here is the last line of defence before the GitHub PATCH body.
"""

from __future__ import annotations

import html

from openbot.domain.repro import ReproOutcome, ReproStatus

# NOTE: ``_ACK_TEMPLATE`` is imported lazily inside ``render_final_comment``.
# A module-level import would create a cycle with ``triage.py`` (which
# imports this module for ``render_thinking_comment`` /
# ``render_final_comment``). The snapshot test in
# ``test_repro_render.py`` re-imports ``_ACK_TEMPLATE`` directly from
# ``triage`` at test-execution time, so the "rename in triage trips the
# snapshot" contract still holds — it just fires on first test run
# rather than at module-import.

# ── Constants ───────────────────────────────────────────────────────────────

# Spec §R3.1 length budget. Most GitHub comment renderers truncate well
# past this, but the sticky-reply flow re-PATCHes the same comment ID so
# a runaway body would cost a maintainer one click to scroll past every
# refresh. Cap aggressively.
_OUTPUT_LENGTH_BUDGET = 4000
_OUTPUT_TRUNCATION_MARKER = "\n\n_…[truncated by renderer]_"

# Excerpt-level clamp inside the rendered comment. The schema already
# trims at 2000 chars; this is a second clamp so the *post-escape*
# excerpt (which can grow ~6x for entity-heavy content) still fits the
# overall budget.
_EXCERPT_RENDER_BUDGET = 1500
_EXCERPT_TRUNCATION_MARKER = "…[truncated]"

# Repro-artifact clamp — the script is meant to be short. Anything past
# this is almost certainly the LLM losing the plot; truncate visibly.
_ARTIFACT_RENDER_BUDGET = 1200


# ── Untrusted-string helpers ────────────────────────────────────────────────


def _safe(value: str | None) -> str:
    """HTML-escape an LLM-produced string for safe interpolation.

    ``html.escape(quote=True)`` covers ``<>&"'`` — the full set GitHub's
    Markdown renderer treats as raw HTML. Returns ``""`` for ``None`` so
    template format-strings stay clean (no ``"None"`` literal).
    """
    if value is None:
        return ""
    return html.escape(value, quote=True)


def _clamp_excerpt(value: str) -> str:
    """Trim an excerpt to the per-excerpt render budget with a marker."""
    if len(value) <= _EXCERPT_RENDER_BUDGET:
        return value
    keep = _EXCERPT_RENDER_BUDGET - len(_EXCERPT_TRUNCATION_MARKER)
    return value[:keep] + _EXCERPT_TRUNCATION_MARKER


def _clamp_artifact(value: str) -> str:
    if len(value) <= _ARTIFACT_RENDER_BUDGET:
        return value
    keep = _ARTIFACT_RENDER_BUDGET - len(_EXCERPT_TRUNCATION_MARKER)
    return value[:keep] + _EXCERPT_TRUNCATION_MARKER


def _final_clamp(body: str) -> str:
    """Hard ceiling on the whole rendered comment.

    Run after templating so all interpolations are visible to the
    truncator. Cuts at a clean line break when possible so the marker
    doesn't land mid-word.
    """
    if len(body) <= _OUTPUT_LENGTH_BUDGET:
        return body
    keep = _OUTPUT_LENGTH_BUDGET - len(_OUTPUT_TRUNCATION_MARKER)
    cut = body[:keep]
    # Prefer cutting at the last newline to avoid mid-fence truncation
    # that would render as broken Markdown.
    last_newline = cut.rfind("\n")
    if last_newline > keep - 200:  # only if the boundary is "close enough"
        cut = cut[:last_newline]
    return cut + _OUTPUT_TRUNCATION_MARKER


def _actor_handle(actor: str | None) -> str:
    """Mirror ``triage.py``'s convention: fall back to ``"there"`` so
    the comment never reads ``@None``."""
    return actor if actor else "there"


# ── Static strings ──────────────────────────────────────────────────────────


# Spec §5.3 — verbatim. The exception class name is deliberately NOT
# interpolated; the audit row carries that for ops.
_AGENT_FAILED_BODY = (
    ":robot: OpenBot's reproduce agent encountered an error and has been "
    "logged. Please re-trigger by re-opening the issue if you'd like to retry."
)


# ── Public renderers ────────────────────────────────────────────────────────


def render_thinking_comment(actor: str | None) -> str:
    """Placeholder body posted before the agent starts.

    The body intentionally signals "I'm working on it" rather than
    promising a specific outcome — the sticky-reply path PATCHes it
    once we have a real ``ReproOutcome``.
    """
    handle = _actor_handle(actor)
    return (
        f":robot: Hi @{handle} — OpenBot is reproducing this issue in a "
        "sandboxed clone of the repo. I'll update this comment with the "
        "result in a minute."
    )


def render_final_comment(
    outcome: ReproOutcome | None,
    *,
    actor: str | None,
    ack_only: bool = False,
) -> str:
    """Final body — either a status template or the ACK fallback.

    Caller contract:
      - ``outcome is None and ack_only is True``  → ACK template.
      - ``outcome is not None``                  → one of four status
        templates dispatched on ``outcome.status``.
      - ``outcome is None and ack_only is False`` → ``ValueError`` (a
        programming error — the use case must pick a branch).
    """
    if outcome is None:
        if not ack_only:
            raise ValueError("render_final_comment requires outcome or ack_only=True; got neither")
        # Deferred import — see module-level NOTE on the triage cycle.
        from openbot.application.use_cases.triage import _ACK_TEMPLATE

        return _ACK_TEMPLATE.format(actor=_actor_handle(actor))

    # Dispatch on status. We don't fall through to a "default" branch
    # because ReproStatus is a closed enum — adding a new variant must
    # be a deliberate change in both the domain and this renderer.
    if outcome.status is ReproStatus.REPRODUCED:
        body = _render_reproduced(outcome)
    elif outcome.status is ReproStatus.NOT_REPRODUCED:
        body = _render_not_reproduced(outcome)
    elif outcome.status is ReproStatus.INSUFFICIENT_INFO:
        body = _render_insufficient_info(outcome)
    elif outcome.status is ReproStatus.AGENT_FAILED:
        body = _AGENT_FAILED_BODY
    else:  # pragma: no cover — guarded by ReproStatus being a closed enum
        raise ValueError(f"unknown ReproStatus: {outcome.status!r}")

    return _final_clamp(body)


# ── Per-status templates ────────────────────────────────────────────────────


def _render_reproduced(outcome: ReproOutcome) -> str:
    """REPRODUCED → success marker + command + fenced excerpt + details
    block carrying the repro script.

    The repro script lives inside a ``<details>`` block so it doesn't
    dominate the comment by default; maintainers expand it when they
    want to grab the script. The filename hint lives in the summary
    line of the details block so it's visible without expanding.
    """
    summary = _safe(outcome.summary)
    command = _safe(outcome.command or "")
    excerpt = _safe(_clamp_excerpt(outcome.output_excerpt))
    hypothesis = _safe(outcome.hypothesis)
    artifact = _safe(_clamp_artifact(outcome.repro_artifact or ""))
    filename = _safe(outcome.repro_artifact_filename or "repro_reproduced.sh")

    return (
        f":white_check_mark: **Reproduced.** {summary}\n"
        f"\n"
        f"**Command:** `{command}` (exit code {outcome.exit_code})\n"
        f"\n"
        f"**Hypothesis:** {hypothesis}\n"
        f"\n"
        f"<details><summary>Failing output</summary>\n"
        f"\n"
        f"```\n{excerpt}\n```\n"
        f"\n"
        f"</details>\n"
        f"\n"
        f"<details><summary>Repro script — save as <code>{filename}</code></summary>\n"
        f"\n"
        f"```bash\n{artifact}\n```\n"
        f"\n"
        f"</details>"
    )


def _render_not_reproduced(outcome: ReproOutcome) -> str:
    """NOT_REPRODUCED → warning marker + what was tried + exit code.

    The maintainer needs to know:
      - what the agent ran (so they don't re-try the same thing),
      - the exit code (0 may mean the bug is already fixed; non-zero
        but different output means the failure mode has shifted).
    """
    summary = _safe(outcome.summary)
    command = _safe(outcome.command or "(no command run)")
    exit_code = outcome.exit_code if outcome.exit_code is not None else "n/a"
    excerpt = _safe(_clamp_excerpt(outcome.output_excerpt))
    hypothesis = _safe(outcome.hypothesis)

    return (
        f":warning: **Could not reproduce.** {summary}\n"
        f"\n"
        f"**Command tried:** `{command}` (exit code {exit_code})\n"
        f"\n"
        f"**Hypothesis:** {hypothesis}\n"
        f"\n"
        f"<details><summary>Observed output</summary>\n"
        f"\n"
        f"```\n{excerpt}\n```\n"
        f"\n"
        f"</details>"
    )


def _render_insufficient_info(outcome: ReproOutcome) -> str:
    """INSUFFICIENT_INFO → conversational marker + missing-info list.

    ``hypothesis`` is expected to be a newline-delimited list of
    missing fields (see system prompt). The renderer turns each
    non-empty line into a Markdown ``- `` list item so it actually
    renders as a list on the comment thread.
    """
    summary = _safe(outcome.summary)
    items = [
        f"- {_safe(line.strip().lstrip('-').strip())}"
        for line in outcome.hypothesis.splitlines()
        if line.strip()
    ]
    list_block = "\n".join(items) if items else "- (no specifics provided)"

    return (
        f":speech_balloon: **Need more info to reproduce.** {summary}\n"
        f"\n"
        f"Could you share:\n"
        f"{list_block}\n"
        f"\n"
        f"Once the issue body has that, re-open and OpenBot will try again."
    )


__all__ = [
    "render_final_comment",
    "render_thinking_comment",
]

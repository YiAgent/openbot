"""A7 — triage sandbox predicate after the reproduce-agent slice.

Companion file to ``test_sandbox_policy.py`` that pins the *positive* form
of the triage rule from the issue-reproduce design spec §A7:

    Triage REQUIRES sandbox iff
        type == "bug" AND has_reproduction_info AND NOT looks_like_spam.

Anything else — feature requests, questions, "other", or bug reports
without repro info — bypasses. The reproduce loop is the *only* triage
codepath that needs a sandbox in v0.1; without that loop, a sandbox is
unused overhead. The old policy was "REQUIRED unless narrowly bypassed",
which over-provisioned for feature/other types.

The matrix lives in this dedicated file so the predicate flip is reviewable
as a single contract change rather than buried among 15 existing rows.
"""

from __future__ import annotations

import pytest

from openbot.application.router import SandboxPolicy
from openbot.application.sandbox_policy import derive_sandbox_policy
from openbot.dispatcher.classifier import TriageClassifierOutput
from openbot.domain.workflows import Feature


def _classifier(
    *,
    type: str = "bug",
    has_reproduction_info: bool = True,
    looks_like_spam: bool = False,
) -> TriageClassifierOutput:
    return TriageClassifierOutput(
        type=type,  # type: ignore[arg-type]
        severity_guess="medium",
        has_reproduction_info=has_reproduction_info,
        looks_like_spam=looks_like_spam,
    )


# ── Truth-table for the A7 predicate ─────────────────────────────────────────
#
# The seven rows below enumerate the cross-product of (type, has_repro,
# looks_like_spam) that matters in practice. Columns we don't vary
# (severity_guess) are held constant in ``_classifier``.


@pytest.mark.parametrize(
    ("triage_type", "has_repro", "looks_like_spam", "expected"),
    [
        # The ONLY REQUIRED row — the reproduce loop has something to chew on.
        ("bug", True, False, SandboxPolicy.REQUIRED),
        # bug without repro: no sandbox; responder asks for repro info.
        ("bug", False, False, SandboxPolicy.NO_SANDBOX),
        # feature request: handler emits a triage comment, never repros.
        ("feature", True, False, SandboxPolicy.NO_SANDBOX),
        ("feature", False, False, SandboxPolicy.NO_SANDBOX),
        # question: classifier_output already carries the answer hint.
        ("question", True, False, SandboxPolicy.NO_SANDBOX),
        ("question", False, False, SandboxPolicy.NO_SANDBOX),
        # spam: never anything to run.
        ("spam", False, False, SandboxPolicy.NO_SANDBOX),
        # "other" type: bag of unknowns; bypass since the reproduce loop
        # only knows how to handle "bug".
        ("other", True, False, SandboxPolicy.NO_SANDBOX),
        # bug-shaped but flagged spam: spam wins.
        ("bug", True, True, SandboxPolicy.NO_SANDBOX),
        # bug without repro AND spam: still NO_SANDBOX.
        ("bug", False, True, SandboxPolicy.NO_SANDBOX),
    ],
)
@pytest.mark.unit
def test_triage_sandbox_predicate(
    triage_type: str,
    has_repro: bool,
    looks_like_spam: bool,
    expected: SandboxPolicy,
) -> None:
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=_classifier(
            type=triage_type,
            has_reproduction_info=has_repro,
            looks_like_spam=looks_like_spam,
        ),
        feature=Feature.TRIAGE,
    )
    assert out is expected, (
        f"({triage_type!r}, repro={has_repro}, spam={looks_like_spam}) "
        f"expected {expected.name}, got {out.name}"
    )


def test_triage_required_iff_bug_and_repro_and_not_spam() -> None:
    """Formalises the predicate: REQUIRED iff `bug AND repro AND NOT spam`.

    This guards against a future refactor that adds a new triage type
    (or a new bypass branch) and forgets to keep the positive form intact.
    """
    triage_types = ("bug", "feature", "question", "spam", "other")
    for triage_type in triage_types:
        for has_repro in (False, True):
            for looks_like_spam in (False, True):
                out = derive_sandbox_policy(
                    static=SandboxPolicy.REQUIRED,
                    classifier_output=_classifier(
                        type=triage_type,
                        has_reproduction_info=has_repro,
                        looks_like_spam=looks_like_spam,
                    ),
                    feature=Feature.TRIAGE,
                )
                wants_sandbox = triage_type == "bug" and has_repro and not looks_like_spam
                expected = SandboxPolicy.REQUIRED if wants_sandbox else SandboxPolicy.NO_SANDBOX
                assert out is expected, (
                    f"type={triage_type} repro={has_repro} spam={looks_like_spam}: "
                    f"want {expected.name}, got {out.name}"
                )

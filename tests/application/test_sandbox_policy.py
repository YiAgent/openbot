"""derive_sandbox_policy — OR-merge of static router + dynamic classifier.

The dispatcher calls this exactly once per event to decide whether to
provision a sandbox. The function is pure so the truth-table is
trivially testable; the dispatcher integration is covered separately
in test_dispatcher.py.
"""

from __future__ import annotations

import pytest

from openbot.application.router import SandboxPolicy
from openbot.application.sandbox_policy import derive_sandbox_policy
from openbot.dispatcher.classifier import (
    ChatClassifierOutput,
    ReviewClassifierOutput,
    TriageClassifierOutput,
)
from openbot.domain.workflows import Feature

# ───── Static NO_SANDBOX always wins ─────


@pytest.mark.parametrize("feature", list(Feature))
def test_static_no_sandbox_always_wins(feature: Feature) -> None:
    # Whatever the classifier says, the router's NO_SANDBOX hint is final.
    # This is the "label-only event already routed to bypass" path.
    out = derive_sandbox_policy(
        static=SandboxPolicy.NO_SANDBOX,
        classifier_output=None,
        feature=feature,
    )
    assert out is SandboxPolicy.NO_SANDBOX


def test_static_no_sandbox_wins_over_engaged_classifier() -> None:
    # Even if the classifier is confident the bot should engage,
    # static NO_SANDBOX from the router (label event) still skips clone.
    out = derive_sandbox_policy(
        static=SandboxPolicy.NO_SANDBOX,
        classifier_output=TriageClassifierOutput(
            type="bug",
            severity_guess="high",
            has_reproduction_info=True,
            looks_like_spam=False,
        ),
        feature=Feature.TRIAGE,
    )
    assert out is SandboxPolicy.NO_SANDBOX


# ───── Fail-open: classifier None → respect static ─────


def test_no_classifier_signal_falls_through_to_required() -> None:
    # Classifier raised / timed out → returns None → fall back to static.
    # This is the failure mode that must NOT block legitimate events.
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=None,
        feature=Feature.TRIAGE,
    )
    assert out is SandboxPolicy.REQUIRED


@pytest.mark.parametrize("feature", list(Feature))
def test_no_classifier_signal_preserves_static(feature: Feature) -> None:
    # Symmetric: REQUIRED stays REQUIRED, NO_SANDBOX stays NO_SANDBOX.
    assert (
        derive_sandbox_policy(
            static=SandboxPolicy.REQUIRED, classifier_output=None, feature=feature
        )
        is SandboxPolicy.REQUIRED
    )
    assert (
        derive_sandbox_policy(
            static=SandboxPolicy.NO_SANDBOX, classifier_output=None, feature=feature
        )
        is SandboxPolicy.NO_SANDBOX
    )


# ───── Triage classifier bypass rules ─────


def test_triage_spam_flag_bypasses() -> None:
    # Single-signal bypass: looks_like_spam=True alone is enough.
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=TriageClassifierOutput(
            type="bug",  # even if type says bug
            severity_guess="medium",
            has_reproduction_info=True,
            looks_like_spam=True,
        ),
        feature=Feature.TRIAGE,
    )
    assert out is SandboxPolicy.NO_SANDBOX


def test_triage_spam_type_without_repro_bypasses() -> None:
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=TriageClassifierOutput(
            type="spam",
            severity_guess="low",
            has_reproduction_info=False,
            looks_like_spam=False,
        ),
        feature=Feature.TRIAGE,
    )
    assert out is SandboxPolicy.NO_SANDBOX


def test_triage_question_without_repro_bypasses() -> None:
    # Questions without reproduction steps don't need a sandbox; the
    # handler will reply asking for repro info.
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=TriageClassifierOutput(
            type="question",
            severity_guess="low",
            has_reproduction_info=False,
            looks_like_spam=False,
        ),
        feature=Feature.TRIAGE,
    )
    assert out is SandboxPolicy.NO_SANDBOX


def test_triage_question_with_repro_still_required() -> None:
    # If the user actually included repro steps, we want the sandbox to
    # validate them — even on type=question.
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=TriageClassifierOutput(
            type="question",
            severity_guess="medium",
            has_reproduction_info=True,
            looks_like_spam=False,
        ),
        feature=Feature.TRIAGE,
    )
    assert out is SandboxPolicy.REQUIRED


def test_triage_bug_with_repro_required() -> None:
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=TriageClassifierOutput(
            type="bug",
            severity_guess="high",
            has_reproduction_info=True,
            looks_like_spam=False,
        ),
        feature=Feature.TRIAGE,
    )
    assert out is SandboxPolicy.REQUIRED


# ───── Chat classifier bypass rules ─────


def test_chat_unclear_intent_bypasses() -> None:
    # Handler will reply asking for clarification — no need for code.
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=ChatClassifierOutput(
            intent="unclear",
            needs_clarification=True,
            scope_hint=None,
        ),
        feature=Feature.CHAT,
    )
    assert out is SandboxPolicy.NO_SANDBOX


def test_chat_out_of_scope_bypasses() -> None:
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=ChatClassifierOutput(
            intent="out_of_scope",
            needs_clarification=False,
            scope_hint="please use Discord for general questions",
        ),
        feature=Feature.CHAT,
    )
    assert out is SandboxPolicy.NO_SANDBOX


def test_chat_readonly_qa_intent_required() -> None:
    # Real question about the codebase → grounded responder needs the
    # sandbox to read files.
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=ChatClassifierOutput(
            intent="readonly_qa",
            needs_clarification=False,
            scope_hint=None,
        ),
        feature=Feature.CHAT,
    )
    assert out is SandboxPolicy.REQUIRED


def test_chat_draft_pr_intent_required() -> None:
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=ChatClassifierOutput(
            intent="draft_pr",
            needs_clarification=False,
            scope_hint=None,
        ),
        feature=Feature.CHAT,
    )
    assert out is SandboxPolicy.REQUIRED


# ───── Review never bypasses on classifier ─────


def test_review_never_bypasses_on_classifier_output() -> None:
    # Review always grounds in-sandbox to produce evidence for findings.
    # The classifier doesn't get a vote here.
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=ReviewClassifierOutput(
            change_size_class="xs",
            touches_security_paths=False,
            is_breaking=False,
            suggested_subagents=("correctness",),
        ),
        feature=Feature.REVIEW,
    )
    assert out is SandboxPolicy.REQUIRED


# ───── Fix never bypasses on classifier ─────


def test_fix_never_bypasses_on_classifier() -> None:
    # Fix events are user-explicit (assignment / cancel label / mention)
    # and the classifier doesn't run for them in v0.1. The function
    # treats any classifier output as inapplicable.
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=None,
        feature=Feature.FIX,
    )
    assert out is SandboxPolicy.REQUIRED


# ───── Cross-feature isolation ─────


def test_chat_classifier_output_on_triage_feature_ignored() -> None:
    # Defensive: if some refactor wires the wrong classifier output
    # under the wrong feature, we don't accidentally bypass.
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=ChatClassifierOutput(
            intent="unclear",
            needs_clarification=True,
            scope_hint=None,
        ),
        feature=Feature.TRIAGE,
    )
    assert out is SandboxPolicy.REQUIRED


def test_triage_classifier_output_on_chat_feature_ignored() -> None:
    out = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=TriageClassifierOutput(
            type="spam",
            severity_guess="low",
            has_reproduction_info=False,
            looks_like_spam=True,
        ),
        feature=Feature.CHAT,
    )
    assert out is SandboxPolicy.REQUIRED

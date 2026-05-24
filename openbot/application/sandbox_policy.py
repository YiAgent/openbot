"""derive_sandbox_policy — OR-merge of static router + dynamic classifier.

The dispatcher calls this once per event, between the existing
``classify_event`` step and the new sandbox provisioning block. Output
drives whether the dispatcher provisions a sandbox before invoking the
handler.

Merge rule (NO_SANDBOX wins):

  1. If the router already marked the route as ``NO_SANDBOX`` (label
     events, cancel direct actions), short-circuit — the classifier
     doesn't get a vote.
  2. If the classifier returned ``None`` (failure / timeout /
     unsupported feature), respect the static decision. This is the
     fail-open contract: classifier outages must not block legitimate
     events.
  3. Otherwise apply per-feature bypass rules:
       - TRIAGE: ``looks_like_spam`` OR (``type ∈ {spam, question}``
         AND ``not has_reproduction_info``) → bypass.
       - CHAT: ``intent ∈ {unclear, out_of_scope}`` → bypass.
       - REVIEW / FIX: never bypass on classifier alone. Review needs
         the sandbox to produce evidence for findings; fix is
         user-explicit and skips the classifier altogether.

Output flows back into ``PreflightContext.classifier_output`` so the
handler can specialize its reply on the bypass path (e.g. "Could you
share reproduction steps?" rather than a silent drop).

Pure function. No I/O. See spec section 'Intent classification
integration' for the design rationale.
"""

from __future__ import annotations

from openbot.application.router import SandboxPolicy
from openbot.dispatcher.classifier import (
    ChatClassifierOutput,
    ClassifierOutput,
    TriageClassifierOutput,
)
from openbot.domain.workflows import Feature

# Triage types that warrant skipping the sandbox when no repro info is
# provided. ``type=bug`` without repro still goes through the sandbox
# so the responder can attempt its own reproduction.
_TRIAGE_BYPASS_TYPES_WITHOUT_REPRO: frozenset[str] = frozenset({"spam", "question"})

# Chat intents that need no code grounding — handler can reply from
# the classifier output alone (ask-for-clarification / out-of-scope
# message).
_CHAT_BYPASS_INTENTS: frozenset[str] = frozenset({"unclear", "out_of_scope"})


def derive_sandbox_policy(
    *,
    static: SandboxPolicy,
    classifier_output: ClassifierOutput | None,
    feature: Feature,
) -> SandboxPolicy:
    """OR-merge the router's static policy with the classifier's signal.

    See module docstring for the merge rule. Returns ``NO_SANDBOX`` if
    either source says skip; otherwise ``REQUIRED``.
    """
    # Rule 1: static NO_SANDBOX is final.
    if static is SandboxPolicy.NO_SANDBOX:
        return SandboxPolicy.NO_SANDBOX

    # Rule 2: fail-open when the classifier didn't produce a signal.
    if classifier_output is None:
        return static

    # Rule 3: per-feature dynamic rules.
    if feature is Feature.TRIAGE and isinstance(classifier_output, TriageClassifierOutput):
        if classifier_output.looks_like_spam:
            return SandboxPolicy.NO_SANDBOX
        if (
            classifier_output.type in _TRIAGE_BYPASS_TYPES_WITHOUT_REPRO
            and not classifier_output.has_reproduction_info
        ):
            return SandboxPolicy.NO_SANDBOX

    if (
        feature is Feature.CHAT
        and isinstance(classifier_output, ChatClassifierOutput)
        and classifier_output.intent in _CHAT_BYPASS_INTENTS
    ):
        return SandboxPolicy.NO_SANDBOX

    # REVIEW always grounds; FIX has no classifier. Mismatched
    # (output, feature) pairs fall through to REQUIRED as a defensive
    # default — a wrong classifier output should never silently drop
    # legitimate work.
    return SandboxPolicy.REQUIRED


__all__ = ["derive_sandbox_policy"]

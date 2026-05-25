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
  3. Otherwise apply per-feature predicates:
       - TRIAGE: REQUIRED iff ``type == "bug" AND has_reproduction_info
         AND NOT looks_like_spam`` (the reproduce loop is the only
         triage codepath that needs a sandbox in v0.1 — see spec §A7).
         Every other triage payload bypasses.
       - CHAT: ``intent ∈ {unclear, out_of_scope}`` → bypass.
       - REVIEW / FIX: never bypass on classifier alone. Review needs
         the sandbox to produce evidence for findings; fix is
         user-explicit and skips the classifier altogether.

Output flows back into ``PreflightContext.classifier_output`` so the
handler can specialize its reply on the bypass path (e.g. "Could you
share reproduction steps?" rather than a silent drop).

Pure function. No I/O. See spec section 'Intent classification
integration' and reproduce-agent spec §A7 for the rationale behind the
TRIAGE predicate flip from "REQUIRED unless narrowly bypassed" to
"REQUIRED iff bug-with-repro".
"""

from __future__ import annotations

from openbot.application.router import SandboxPolicy
from openbot.dispatcher.classifier import (
    ChatClassifierOutput,
    ClassifierOutput,
    TriageClassifierOutput,
)
from openbot.domain.workflows import Feature

# Chat intents that need no code grounding — handler can reply from
# the classifier output alone (ask-for-clarification / out-of-scope
# message).
_CHAT_BYPASS_INTENTS: frozenset[str] = frozenset({"unclear", "out_of_scope"})


def _triage_wants_sandbox(out: TriageClassifierOutput) -> bool:
    """A7 predicate — only bug reports with repro info justify the sandbox.

    The reproduce loop is the sole triage codepath in v0.1 that needs to
    execute anything against the repo. Without ``has_reproduction_info``
    the loop has nothing to chew on (the responder will ask for repro
    steps instead), and non-bug types never enter the loop at all.
    ``looks_like_spam`` short-circuits regardless of type/repro.
    """
    return out.type == "bug" and out.has_reproduction_info and not out.looks_like_spam


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

    # Rule 3: per-feature predicates.
    if feature is Feature.TRIAGE and isinstance(classifier_output, TriageClassifierOutput):
        return (
            SandboxPolicy.REQUIRED
            if _triage_wants_sandbox(classifier_output)
            else SandboxPolicy.NO_SANDBOX
        )

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

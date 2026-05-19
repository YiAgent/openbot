"""Application use cases — PRD §4.

All application-layer operations live here:

  - ``ingest_webhook``  Entry use case: receive a webhook event, dedup,
                        route, and enqueue (or background-dispatch).
  - ``triage``          Feature: acknowledge and triage an opened issue.
  - ``review``          Feature: acknowledge and queue a PR review.
  - ``fix``             Feature: acknowledge and queue an issue fix.
  - ``chat``            Feature: respond to an @openbot mention.
  - ``debug_echo``      Dev/test handler: echo event trace when
                        OPENBOT_DEBUG_ECHO=1 (replaces real features).
"""

from openbot.application.use_cases.chat import maybe_run_chat
from openbot.application.use_cases.debug_echo import debug_echo_handler
from openbot.application.use_cases.fix import maybe_run_fix
from openbot.application.use_cases.review import maybe_run_review
from openbot.application.use_cases.triage import maybe_run_triage

__all__ = [
    "debug_echo_handler",
    "maybe_run_chat",
    "maybe_run_fix",
    "maybe_run_review",
    "maybe_run_triage",
]

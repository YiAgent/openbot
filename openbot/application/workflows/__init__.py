"""High-level workflows — PRD §4 / harness spec §3 M12.

Each feature has one entry point with the `maybe_run_*(ctx)` signature
where `ctx` is a `PreflightContext` (pre-flight gates have already run).

v0.1 Week 2 ships all four as ACK-only stubs. Real LLM agents land
once the agent slice arrives.
"""

from openbot.application.workflows.chat import maybe_run_chat
from openbot.application.workflows.fix import maybe_run_fix
from openbot.application.workflows.review import maybe_run_review
from openbot.application.workflows.triage import maybe_run_triage

__all__ = [
    "maybe_run_chat",
    "maybe_run_fix",
    "maybe_run_review",
    "maybe_run_triage",
]

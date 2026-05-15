"""High-level workflows. PRD §4 maps each to one feature.

v0.1 Week 1 ships only the triage ACK slice; review/fix/chat land in
upcoming commits.
"""

from openbot.workflows.triage import maybe_run_triage

__all__ = ["maybe_run_triage"]

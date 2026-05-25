"""Factory functions for test data — events, payloads, decisions, runs.

Builders take keyword-only args with sensible defaults. Tests read as
`event = build_issue_opened_event(body="foo")` rather than 30-line
state-machine harness assembly."""

from __future__ import annotations

__all__: list[str] = []

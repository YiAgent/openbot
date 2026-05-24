"""In-memory LangGraph checkpointer for agent integration tests.

Wraps langgraph's MemorySaver so callers don't have to know which
checkpointer class to import. Returned object satisfies
langgraph.checkpoint.base.BaseCheckpointSaver."""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver


def build_inmemory_checkpointer() -> MemorySaver:
    """Return a fresh MemorySaver. Callers own its lifetime."""
    return MemorySaver()


__all__ = ["build_inmemory_checkpointer"]

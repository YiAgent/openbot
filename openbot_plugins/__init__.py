"""OpenBot community plugins.

Plugins are LangChain StructuredTool instances registered at import time.
The agent discovers them via ``get_all_plugins()`` and merges them into
the tool list for the relevant workflow.

Contributing: see CONTRIBUTING.md for the full PR checklist.
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.tools import StructuredTool

_REGISTRY: list[StructuredTool] = []


def register(tool: StructuredTool) -> None:
    """Register a plugin tool. Called at module level in each plugin file."""
    _REGISTRY.append(tool)


def get_all_plugins() -> Sequence[StructuredTool]:
    """Return all registered plugin tools (read-only view)."""
    return tuple(_REGISTRY)


def reset_registry() -> None:
    """Clear all registered plugins. Used in tests only."""
    _REGISTRY.clear()


__all__ = ["get_all_plugins", "register", "reset_registry"]

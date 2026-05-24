"""Full-stack E2E assembler — single-process, no real network.

Wires the complete pipeline:
  webhook → queue → worker → use case → channel adapter

Only the GitHub channel adapter is fake; everything else runs real
code paths. Uses fakeredis + aiosqlite for the persistence layer.

TODO (Phase 7): implement E2EStack and build_e2e_stack() once the
use cases, dispatcher, and worker are concrete enough to wire.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class E2EStack:
    """Placeholder: wired single-process stack for E2E tests.

    Phase 7 will replace this stub with the real assembly.
    """


async def build_e2e_stack() -> E2EStack:
    """Assemble and return a fresh E2EStack.

    Phase 7 will implement the real wiring. Until then callers get
    an empty stack so the conftest fixture compiles without error.
    """
    return E2EStack()


__all__ = ["E2EStack", "build_e2e_stack"]

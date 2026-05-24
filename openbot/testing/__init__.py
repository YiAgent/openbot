"""Test doubles for OpenBot.

Importable via `from openbot.testing import FakeQueue, build_issue_opened_event`.
Requires `pip install openbot[testing]`. Production install does NOT bundle
fakeredis / aiosqlite / vcrpy / respx — those are in the `testing` extra.

Layer rule: production code in openbot.{domain,application,infrastructure,
entrypoints,core,dispatcher,evaluation} MUST NOT import from this package.
This is enforced by import-linter (.importlinter contract `no-testing-in-runtime`).
The `evals/` tree IS allowed to import from here.
"""

from __future__ import annotations

from openbot.testing.fakes import EnqueueRecord, FakeQueue

__all__ = ["EnqueueRecord", "FakeQueue"]

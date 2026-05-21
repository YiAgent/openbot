"""Pure value objects for the fix workflow — slice C.

Why this lives in the domain layer (mirrors ``openbot/domain/review.py``):

  - The use case (``maybe_run_fix``) decides PR vs comment based on
    ``attempt.tests_passed`` and ``outcome.error`` without touching
    pydantic, langchain, or HTTP shapes.
  - The responder (``DeepAgentsFixResponder``) returns these types, not
    the pydantic schema used to coerce LLM output. Pydantic stops at
    ``openbot/infrastructure/agents/_fix_schema.py``.

Invariants are asserted in tests (not in ``__post_init__``) because
partial outcomes do exist — tests passed but ``open_pull_request`` failed
must still be representable as ``FixOutcome(attempt=..., pr_url=None,
error="...")``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FixAttempt:
    """One reasoning pass through the fix loop.

    Holds everything the use case needs to decide between PR vs comment.

    ``files_changed`` is a tuple (not list) so consumers can't mutate the
    record after the responder returns it. ``test_output`` is the raw
    stdout+stderr from the test run, truncated by the responder to keep
    GitHub comments readable.
    """

    summary: str
    files_changed: tuple[str, ...]
    tests_passed: bool
    test_command: str
    test_output: str
    diff: str


@dataclass(frozen=True, slots=True)
class FixOutcome:
    """End-to-end result of ``maybe_run_fix``.

    ``pr_url`` is set only when ``attempt.tests_passed`` is True and the
    PR was opened successfully. ``error`` is set only when something
    raised before completing — both can be None on the tests-failed path
    (legitimate terminal state).
    """

    attempt: FixAttempt
    pr_url: str | None = None
    error: str | None = None


__all__ = ["FixAttempt", "FixOutcome"]

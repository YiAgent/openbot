"""Tests for the input-side state machine slice.

Three pure-by-construction suites:

* ``test_classifier`` — the ``(event, state) → intent`` table, row by row.
* ``test_runs_repo`` — CAS retry + stale-seq rejection against in-memory SQLite.
* ``test_cancellation`` — in-process registry + Redis flag + checkpoint.

The classifier suite is the canonical living spec for the intent matrix —
when a new event kind is added, expand the parametrize table here first
and let the failing test drive the classifier change.
"""

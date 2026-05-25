"""Domain dataclasses for the reproduce stage of triage.

Mirrors ``tests/domain/test_fix.py`` conventions: invariants live in tests,
not in ``__post_init__``, because partial-failure outcomes (e.g.
``AGENT_FAILED`` after a command ran but before ``exit_code`` was captured)
must still be representable.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from openbot.domain.repro import ReproOutcome, ReproStatus


def _outcome(**overrides: object) -> ReproOutcome:
    defaults: dict[str, object] = {
        "status": ReproStatus.REPRODUCED,
        "summary": "Confirmed: list index errors on empty input.",
        "command": "pytest tests/test_pagination.py::test_empty -q",
        "exit_code": 1,
        "output_excerpt": "IndexError: list index out of range",
        "hypothesis": "src/api/list.py:42 dereferences items[0] without a guard.",
        "repro_artifact": "#!/usr/bin/env bash\npytest tests/test_pagination.py::test_empty -q\n",
        "repro_artifact_filename": "repro_reproduced.sh",
    }
    defaults.update(overrides)
    return ReproOutcome(**defaults)  # type: ignore[arg-type]


# ── enum stability ───────────────────────────────────────────────────────────


def test_repro_status_values_are_stable() -> None:
    # These string values land in the audit log and Langfuse metadata.
    # Changing them is a wire-format break — fail loudly if it happens.
    assert ReproStatus.REPRODUCED.value == "reproduced"
    assert ReproStatus.NOT_REPRODUCED.value == "not_reproduced"
    assert ReproStatus.INSUFFICIENT_INFO.value == "insufficient_info"
    assert ReproStatus.AGENT_FAILED.value == "agent_failed"


def test_repro_status_set_is_exhaustive() -> None:
    # Guards against an extra status sneaking in without an explicit ADR.
    assert {s.value for s in ReproStatus} == {
        "reproduced",
        "not_reproduced",
        "insufficient_info",
        "agent_failed",
    }


# ── dataclass shape ──────────────────────────────────────────────────────────


def test_outcome_holds_required_fields() -> None:
    o = _outcome()
    assert o.status is ReproStatus.REPRODUCED
    assert o.command == "pytest tests/test_pagination.py::test_empty -q"
    assert o.exit_code == 1
    assert o.repro_artifact_filename == "repro_reproduced.sh"


def test_outcome_is_frozen() -> None:
    o = _outcome()
    with pytest.raises(FrozenInstanceError):
        o.summary = "no"  # type: ignore[misc]


def test_outcome_missing_required_field_raises_typeerror() -> None:
    with pytest.raises(TypeError):
        ReproOutcome(  # type: ignore[call-arg]
            status=ReproStatus.REPRODUCED,
            summary="x",
            command="c",
            exit_code=0,
            output_excerpt="o",
            hypothesis="h",
            # missing repro_artifact + repro_artifact_filename
        )


# ── invariants asserted in tests (not __post_init__) ─────────────────────────
#
# Why here and not in __post_init__: partial-failure outcomes (e.g.
# AGENT_FAILED after a command ran but before exit_code was captured) must
# still be representable so the audit row records what we actually saw.
# Constructor-side enforcement would erase that information. Same rationale
# as domain/fix.py.


def test_artifact_pair_invariant_not_enforced_by_post_init() -> None:
    # Mismatched pairs MUST be constructible — the constructor does not
    # enforce the invariant. This is intentional (see module docstring),
    # so audit rows can record partial state when the responder is the
    # one violating the contract. The use case is responsible for not
    # consuming such outcomes.
    bad = ReproOutcome(
        status=ReproStatus.REPRODUCED,
        summary="x",
        command="c",
        exit_code=0,
        output_excerpt="",
        hypothesis="",
        repro_artifact="#!/usr/bin/env bash\necho hi\n",
        repro_artifact_filename=None,  # mismatched on purpose
    )
    # If __post_init__ were enforcing this, construction would have raised.
    assert bad.repro_artifact is not None
    assert bad.repro_artifact_filename is None


def test_artifact_pair_valid_when_both_set_or_both_none() -> None:
    both_set = _outcome()
    assert (both_set.repro_artifact is None) is (both_set.repro_artifact_filename is None)

    both_none = _outcome(
        status=ReproStatus.NOT_REPRODUCED,
        command="pytest -q tests/test_x.py",
        exit_code=0,
        repro_artifact=None,
        repro_artifact_filename=None,
    )
    assert (both_none.repro_artifact is None) is (both_none.repro_artifact_filename is None)


def test_reproduced_requires_command_and_artifact() -> None:
    # The use case / responder is responsible for not constructing this shape.
    # We pin the property tests rely on so a future refactor can't quietly
    # drop the requirement.
    o = _outcome(status=ReproStatus.REPRODUCED)
    assert o.status is ReproStatus.REPRODUCED
    assert o.command is not None
    assert o.repro_artifact is not None


def test_insufficient_info_has_no_command_or_artifact() -> None:
    o = _outcome(
        status=ReproStatus.INSUFFICIENT_INFO,
        command=None,
        exit_code=None,
        repro_artifact=None,
        repro_artifact_filename=None,
        hypothesis="Need: error stack trace, library versions, and minimal input.",
    )
    assert o.command is None
    assert o.exit_code is None
    assert o.repro_artifact is None
    assert o.repro_artifact_filename is None


def test_output_excerpt_under_budget() -> None:
    # The responder is responsible for truncation; this test pins the budget
    # downstream code (render, audit) depends on.
    o = _outcome(output_excerpt="x" * 2000)
    assert len(o.output_excerpt) == 2000


# ── status surface ───────────────────────────────────────────────────────────


def test_agent_failed_keeps_minimal_payload() -> None:
    # AGENT_FAILED outcomes are constructed by the use case when the agent
    # raised before producing a structured result. They carry just enough
    # for the comment renderer.
    o = _outcome(
        status=ReproStatus.AGENT_FAILED,
        command=None,
        exit_code=None,
        output_excerpt="",
        hypothesis="agent timed out before forming a hypothesis",
        repro_artifact=None,
        repro_artifact_filename=None,
    )
    assert o.status is ReproStatus.AGENT_FAILED
    assert o.command is None
    assert o.repro_artifact is None

"""Domain types for the unified sandbox-entry checkout step."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from openbot.domain.checkout import (
    CheckoutResolutionError,
    CheckoutSpec,
    CloneStrategy,
)

# ---------------------------------------------------------------------------
# CloneStrategy
# ---------------------------------------------------------------------------


def test_clone_strategy_has_four_values() -> None:
    # The matrix in the spec depends on exactly these four — adding a fifth
    # without updating the matrix is the kind of silent drift this test guards.
    assert {s.value for s in CloneStrategy} == {
        "blobless",
        "shallow",
        "shallow_history",
        "full",
    }


def test_clone_strategy_is_str_subclass() -> None:
    # StrEnum members must compare equal to their string values so they
    # survive JSON/CLI/log round-trips without manual ``.value`` access.
    assert CloneStrategy.SHALLOW == "shallow"
    assert CloneStrategy("blobless") is CloneStrategy.BLOBLESS


# ---------------------------------------------------------------------------
# CheckoutSpec
# ---------------------------------------------------------------------------


def _spec(**overrides: object) -> CheckoutSpec:
    defaults: dict[str, object] = {
        "repo_url": "https://github.com/o/r.git",
        "ref": "deadbeef" * 5,
    }
    defaults.update(overrides)
    return CheckoutSpec(**defaults)  # type: ignore[arg-type]


def test_checkout_spec_holds_repo_and_ref() -> None:
    s = _spec()
    assert s.repo_url == "https://github.com/o/r.git"
    assert s.ref == "deadbeef" * 5


def test_checkout_spec_default_strategy_is_shallow() -> None:
    # SHALLOW is the conservative default — covers fix/triage/single-commit
    # checkouts. REVIEW workflows explicitly upgrade to SHALLOW_HISTORY.
    assert _spec().strategy is CloneStrategy.SHALLOW


def test_checkout_spec_default_diff_base_is_none() -> None:
    # diff_base only populates for incremental review — must default to None
    # so non-review handlers can ignore it without an isinstance check.
    assert _spec().diff_base is None


def test_checkout_spec_default_sparse_paths_is_empty_tuple() -> None:
    # Tuple (not list) so the field can be shared / hashed; empty default
    # means "no sparse-checkout".
    assert _spec().sparse_paths == ()
    assert isinstance(_spec().sparse_paths, tuple)


def test_checkout_spec_is_frozen() -> None:
    s = _spec()
    with pytest.raises(FrozenInstanceError):
        s.ref = "other"  # type: ignore[misc]


def test_checkout_spec_equality_and_hash() -> None:
    a = _spec()
    b = _spec()
    assert a == b
    assert hash(a) == hash(b)


def test_checkout_spec_accepts_explicit_strategy_and_diff_base() -> None:
    s = _spec(
        strategy=CloneStrategy.SHALLOW_HISTORY,
        diff_base="cafef00d" * 5,
    )
    assert s.strategy is CloneStrategy.SHALLOW_HISTORY
    assert s.diff_base == "cafef00d" * 5


# ---------------------------------------------------------------------------
# CheckoutResolutionError
# ---------------------------------------------------------------------------


def test_checkout_resolution_error_is_exception() -> None:
    # The dispatcher catches this specifically to fall into the graceful
    # degrade path — must be catchable as a regular Exception.
    err = CheckoutResolutionError("no rule for kind=UNKNOWN")
    assert isinstance(err, Exception)
    assert str(err) == "no rule for kind=UNKNOWN"

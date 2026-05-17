"""Pre-flight chain ordering — locked by spec §3 M3 + plan §5.

Any change to ``build_preflight_chain`` MUST be paired with a spec
amendment. This test is the early-warning system: if someone tries
to silently insert a new middleware or shuffle the order, the test
fires before the change merges.
"""

from __future__ import annotations

from openbot.dispatch import build_preflight_chain

# The locked order. Adding a middleware means amending spec §3 M3 + this
# tuple in one PR. Hardcoded explicit string list (not derived) so the
# diff in code review shows exactly what changed.
_EXPECTED_CHAIN_ORDER: tuple[str, ...] = (
    "sanitize_inputs",
    "kill_switch",
    "feature_toggle",
    "cancel_label",
    "cancel_comment",
    "fork_pr_gate",
    "actor_role",
    "rate_limit",
    "budget",
    "audit_start",
)


def test_chain_order_matches_spec() -> None:
    """The middleware names + order must match spec §3 M3 + plan §5."""
    actual = tuple(mw.name for mw in build_preflight_chain())
    assert actual == _EXPECTED_CHAIN_ORDER, (
        "Pre-flight chain order changed. Update spec §3 M3 + the "
        f"_EXPECTED_CHAIN_ORDER tuple in this test.\n"
        f"  expected: {_EXPECTED_CHAIN_ORDER}\n"
        f"  actual:   {actual}"
    )


def test_chain_has_no_duplicate_middlewares() -> None:
    """A middleware appearing twice would always BLOCK at its second hit
    (e.g. duplicate audit writes, repeated rate-limit increments)."""
    names = [mw.name for mw in build_preflight_chain()]
    assert len(names) == len(set(names)), f"duplicate middleware in chain: {names}"


def test_sanitize_is_first() -> None:
    """``sanitized_event(ctx)`` works only if sanitize ran first."""
    chain = build_preflight_chain()
    assert chain[0].name == "sanitize_inputs"


def test_audit_start_is_last() -> None:
    """STARTED must be the final preflight gate; otherwise a downstream
    middleware that BLOCKS would still leave a STARTED row + no pair."""
    chain = build_preflight_chain()
    assert chain[-1].name == "audit_start"

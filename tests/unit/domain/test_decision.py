"""Unit tests for openbot.domain.intents.

Tests: State/Intent enum values, EventClassification immutability,
and that the distinct state/intent types are not interchangeable.
"""

from __future__ import annotations

import pytest

from openbot.domain.intents import EventClassification, Intent, State

# ── State enum ────────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestState:
    def test_all_states_are_strings(self) -> None:
        for state in State:
            assert isinstance(state.value, str)

    def test_four_states_defined(self) -> None:
        assert len(State) == 4

    def test_canonical_values(self) -> None:
        assert State.IDLE == "idle"
        assert State.RUNNING == "running"
        assert State.CANCELLED == "cancelled"
        assert State.CLOSED == "closed"


# ── Intent enum ───────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestIntent:
    def test_four_intents_defined(self) -> None:
        assert len(Intent) == 4

    def test_canonical_values(self) -> None:
        assert Intent.START == "start"
        assert Intent.SUPERSEDE == "supersede"
        assert Intent.CANCEL == "cancel"
        assert Intent.IGNORE == "ignore"

    def test_intent_and_state_not_equal(self) -> None:
        """Intent and State are distinct StrEnum types — must not mix."""
        # IDLE ≠ IGNORE even if values matched
        assert Intent.IGNORE != State.IDLE


# ── EventClassification ───────────────────────────────────────────────────────


@pytest.mark.unit
class TestEventClassification:
    def test_minimal_construction(self) -> None:
        ec = EventClassification(intent=Intent.START, next_state=State.RUNNING)
        assert ec.intent is Intent.START
        assert ec.next_state is State.RUNNING
        assert ec.reason is None

    def test_reason_stored(self) -> None:
        ec = EventClassification(
            intent=Intent.IGNORE,
            next_state=State.IDLE,
            reason="bot_actor",
        )
        assert ec.reason == "bot_actor"

    def test_frozen_raises_on_mutation(self) -> None:
        ec = EventClassification(intent=Intent.CANCEL, next_state=State.CANCELLED)
        with pytest.raises((AttributeError, TypeError)):
            ec.reason = "changed"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "intent, next_state",
        [
            (Intent.START, State.RUNNING),
            (Intent.SUPERSEDE, State.RUNNING),
            (Intent.CANCEL, State.CANCELLED),
            (Intent.IGNORE, State.IDLE),
        ],
    )
    def test_canonical_combinations(self, intent: Intent, next_state: State) -> None:
        ec = EventClassification(intent=intent, next_state=next_state)
        assert ec.intent is intent
        assert ec.next_state is next_state

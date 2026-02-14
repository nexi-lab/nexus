"""Tests for the generic state machine.

Issue #1359 Phase 0: Exhaustive matrix of valid/invalid transitions.
"""

from __future__ import annotations

import pytest

from nexus.services.governance.approval.state_machine import InvalidTransitionError, StateMachine


@pytest.fixture
def approval_sm() -> StateMachine:
    """Standard approval state machine."""
    return StateMachine(
        {
            "pending": frozenset({"approved", "rejected", "expired"}),
            "approved": frozenset(),
            "rejected": frozenset(),
            "expired": frozenset(),
        }
    )


@pytest.fixture
def dispute_sm() -> StateMachine:
    """Dispute state machine (from DisputeService)."""
    return StateMachine(
        {
            "filed": frozenset({"auto_mediating", "dismissed"}),
            "auto_mediating": frozenset({"resolved", "dismissed"}),
            "resolved": frozenset(),
            "dismissed": frozenset(),
        }
    )


class TestStateMachineInit:
    """Tests for StateMachine initialization."""

    def test_valid_transitions(self, approval_sm: StateMachine) -> None:
        assert approval_sm.states == frozenset({"pending", "approved", "rejected", "expired"})

    def test_terminal_states(self, approval_sm: StateMachine) -> None:
        assert approval_sm.terminal_states == frozenset({"approved", "rejected", "expired"})

    def test_undefined_target_raises(self) -> None:
        with pytest.raises(ValueError, match="not defined"):
            StateMachine({"a": frozenset({"b"})})

    def test_empty_state_machine(self) -> None:
        sm = StateMachine({})
        assert sm.states == frozenset()
        assert sm.terminal_states == frozenset()


class TestStateMachineTransitions:
    """Tests for state transitions."""

    def test_valid_transition_pending_to_approved(self, approval_sm: StateMachine) -> None:
        result = approval_sm.transition("pending", "approved")
        assert result == "approved"

    def test_valid_transition_pending_to_rejected(self, approval_sm: StateMachine) -> None:
        result = approval_sm.transition("pending", "rejected")
        assert result == "rejected"

    def test_valid_transition_pending_to_expired(self, approval_sm: StateMachine) -> None:
        result = approval_sm.transition("pending", "expired")
        assert result == "expired"

    def test_invalid_transition_approved_to_pending(self, approval_sm: StateMachine) -> None:
        with pytest.raises(InvalidTransitionError) as exc_info:
            approval_sm.transition("approved", "pending")
        assert exc_info.value.current == "approved"
        assert exc_info.value.target == "pending"
        assert exc_info.value.valid == frozenset()

    def test_invalid_transition_rejected_to_approved(self, approval_sm: StateMachine) -> None:
        with pytest.raises(InvalidTransitionError):
            approval_sm.transition("rejected", "approved")

    def test_invalid_transition_expired_to_approved(self, approval_sm: StateMachine) -> None:
        with pytest.raises(InvalidTransitionError):
            approval_sm.transition("expired", "approved")

    def test_unknown_current_state_raises(self, approval_sm: StateMachine) -> None:
        with pytest.raises(KeyError, match="Unknown state"):
            approval_sm.transition("unknown", "approved")

    def test_self_transition_invalid(self, approval_sm: StateMachine) -> None:
        with pytest.raises(InvalidTransitionError):
            approval_sm.transition("pending", "pending")


class TestDisputeStateMachine:
    """Tests for dispute-specific state machine."""

    def test_filed_to_auto_mediating(self, dispute_sm: StateMachine) -> None:
        assert dispute_sm.transition("filed", "auto_mediating") == "auto_mediating"

    def test_filed_to_dismissed(self, dispute_sm: StateMachine) -> None:
        assert dispute_sm.transition("filed", "dismissed") == "dismissed"

    def test_auto_mediating_to_resolved(self, dispute_sm: StateMachine) -> None:
        assert dispute_sm.transition("auto_mediating", "resolved") == "resolved"

    def test_auto_mediating_to_dismissed(self, dispute_sm: StateMachine) -> None:
        assert dispute_sm.transition("auto_mediating", "dismissed") == "dismissed"

    def test_filed_to_resolved_invalid(self, dispute_sm: StateMachine) -> None:
        with pytest.raises(InvalidTransitionError):
            dispute_sm.transition("filed", "resolved")

    def test_resolved_is_terminal(self, dispute_sm: StateMachine) -> None:
        assert dispute_sm.is_terminal("resolved")

    def test_dismissed_is_terminal(self, dispute_sm: StateMachine) -> None:
        assert dispute_sm.is_terminal("dismissed")

    def test_filed_is_not_terminal(self, dispute_sm: StateMachine) -> None:
        assert not dispute_sm.is_terminal("filed")


class TestStateMachineHelpers:
    """Tests for helper methods."""

    def test_valid_targets(self, approval_sm: StateMachine) -> None:
        targets = approval_sm.valid_targets("pending")
        assert targets == frozenset({"approved", "rejected", "expired"})

    def test_valid_targets_terminal(self, approval_sm: StateMachine) -> None:
        assert approval_sm.valid_targets("approved") == frozenset()

    def test_to_dict(self, approval_sm: StateMachine) -> None:
        d = approval_sm.to_dict()
        assert d["pending"] == ["approved", "expired", "rejected"]
        assert d["approved"] == []

    def test_repr(self, approval_sm: StateMachine) -> None:
        r = repr(approval_sm)
        assert "StateMachine" in r
        assert "terminal" in r

    def test_equality(self) -> None:
        sm1 = StateMachine({"a": frozenset({"b"}), "b": frozenset()})
        sm2 = StateMachine({"a": frozenset({"b"}), "b": frozenset()})
        assert sm1 == sm2

    def test_inequality(self) -> None:
        sm1 = StateMachine({"a": frozenset({"b"}), "b": frozenset()})
        sm2 = StateMachine({"a": frozenset(), "b": frozenset()})
        assert sm1 != sm2

    def test_hash(self) -> None:
        sm1 = StateMachine({"a": frozenset({"b"}), "b": frozenset()})
        sm2 = StateMachine({"a": frozenset({"b"}), "b": frozenset()})
        assert hash(sm1) == hash(sm2)

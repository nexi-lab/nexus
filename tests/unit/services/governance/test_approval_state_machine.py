"""Unit tests for the generic StateMachine and approval types.

Tests state transitions, terminal states, serialization,
and error handling in the state machine.
"""

from __future__ import annotations

import pytest

from nexus.services.governance.approval.state_machine import (
    InvalidTransitionError,
    StateMachine,
)
from nexus.services.governance.approval.types import (
    ApprovalStatus,
    ApprovalTimestamps,
    ExpiryPolicy,
)

# ---------------------------------------------------------------------------
# StateMachine basics
# ---------------------------------------------------------------------------


class TestStateMachineCreation:
    """Tests for StateMachine construction and validation."""

    def test_valid_transitions(self) -> None:
        sm = StateMachine(
            {
                "pending": frozenset({"approved", "rejected"}),
                "approved": frozenset(),
                "rejected": frozenset(),
            }
        )
        assert sm.states == frozenset({"pending", "approved", "rejected"})

    def test_undefined_target_raises(self) -> None:
        with pytest.raises(ValueError, match="not defined in transitions"):
            StateMachine(
                {
                    "pending": frozenset({"approved", "unknown_state"}),
                    "approved": frozenset(),
                }
            )

    def test_single_state_no_transitions(self) -> None:
        sm = StateMachine({"only": frozenset()})
        assert sm.states == frozenset({"only"})
        assert sm.terminal_states == frozenset({"only"})


# ---------------------------------------------------------------------------
# State properties
# ---------------------------------------------------------------------------


class TestStateMachineProperties:
    """Tests for states and terminal_states properties."""

    @pytest.fixture()
    def approval_sm(self) -> StateMachine:
        return StateMachine(
            {
                "pending": frozenset({"approved", "rejected", "expired"}),
                "approved": frozenset(),
                "rejected": frozenset(),
                "expired": frozenset(),
            }
        )

    def test_states(self, approval_sm: StateMachine) -> None:
        assert approval_sm.states == frozenset({"pending", "approved", "rejected", "expired"})

    def test_terminal_states(self, approval_sm: StateMachine) -> None:
        assert approval_sm.terminal_states == frozenset({"approved", "rejected", "expired"})

    def test_is_terminal(self, approval_sm: StateMachine) -> None:
        assert approval_sm.is_terminal("approved") is True
        assert approval_sm.is_terminal("pending") is False


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


class TestStateMachineTransitions:
    """Tests for transition validation."""

    @pytest.fixture()
    def sm(self) -> StateMachine:
        return StateMachine(
            {
                "pending": frozenset({"approved", "rejected"}),
                "approved": frozenset(),
                "rejected": frozenset(),
            }
        )

    def test_valid_transition(self, sm: StateMachine) -> None:
        result = sm.transition("pending", "approved")
        assert result == "approved"

    def test_invalid_transition_raises(self, sm: StateMachine) -> None:
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.transition("approved", "pending")
        assert exc_info.value.current == "approved"
        assert exc_info.value.target == "pending"

    def test_unknown_state_raises_key_error(self, sm: StateMachine) -> None:
        with pytest.raises(KeyError, match="Unknown state"):
            sm.transition("nonexistent", "approved")

    def test_valid_targets(self, sm: StateMachine) -> None:
        targets = sm.valid_targets("pending")
        assert targets == frozenset({"approved", "rejected"})

    def test_valid_targets_terminal(self, sm: StateMachine) -> None:
        targets = sm.valid_targets("approved")
        assert targets == frozenset()

    def test_valid_targets_unknown_raises(self, sm: StateMachine) -> None:
        with pytest.raises(KeyError, match="Unknown state"):
            sm.valid_targets("nonexistent")


# ---------------------------------------------------------------------------
# Serialization and equality
# ---------------------------------------------------------------------------


class TestStateMachineSerialization:
    """Tests for to_dict, __eq__, __hash__, __repr__."""

    def test_to_dict(self) -> None:
        sm = StateMachine(
            {
                "pending": frozenset({"approved", "rejected"}),
                "approved": frozenset(),
                "rejected": frozenset(),
            }
        )
        d = sm.to_dict()
        assert d["pending"] == ["approved", "rejected"]
        assert d["approved"] == []
        assert d["rejected"] == []

    def test_equality(self) -> None:
        sm1 = StateMachine(
            {
                "a": frozenset({"b"}),
                "b": frozenset(),
            }
        )
        sm2 = StateMachine(
            {
                "a": frozenset({"b"}),
                "b": frozenset(),
            }
        )
        assert sm1 == sm2

    def test_inequality(self) -> None:
        sm1 = StateMachine(
            {
                "a": frozenset({"b"}),
                "b": frozenset(),
            }
        )
        sm2 = StateMachine(
            {
                "a": frozenset({"b", "c"}),
                "b": frozenset(),
                "c": frozenset(),
            }
        )
        assert sm1 != sm2

    def test_not_equal_to_non_state_machine(self) -> None:
        sm = StateMachine({"a": frozenset()})
        assert sm != "not a state machine"

    def test_hashable(self) -> None:
        sm = StateMachine({"a": frozenset({"b"}), "b": frozenset()})
        # Should be hashable and usable in sets
        s = {sm}
        assert sm in s

    def test_repr(self) -> None:
        sm = StateMachine(
            {
                "pending": frozenset({"done"}),
                "done": frozenset(),
            }
        )
        r = repr(sm)
        assert "StateMachine" in r
        assert "done" in r
        assert "pending" in r


# ---------------------------------------------------------------------------
# InvalidTransitionError
# ---------------------------------------------------------------------------


class TestInvalidTransitionError:
    """Tests for InvalidTransitionError."""

    def test_attributes(self) -> None:
        err = InvalidTransitionError("current", "target", frozenset({"a", "b"}))
        assert err.current == "current"
        assert err.target == "target"
        assert err.valid == frozenset({"a", "b"})

    def test_message_format(self) -> None:
        err = InvalidTransitionError("pending", "deleted", frozenset({"approved"}))
        msg = str(err)
        assert "pending" in msg
        assert "deleted" in msg
        assert "approved" in msg

    def test_empty_valid_set(self) -> None:
        err = InvalidTransitionError("done", "pending", frozenset())
        msg = str(err)
        assert "terminal" in msg.lower() or "none" in msg.lower()


# ---------------------------------------------------------------------------
# ApprovalStatus, ApprovalTimestamps, ExpiryPolicy types
# ---------------------------------------------------------------------------


class TestApprovalTypes:
    """Tests for approval type dataclasses and enums."""

    def test_approval_status_values(self) -> None:
        assert ApprovalStatus.PENDING == "pending"
        assert ApprovalStatus.APPROVED == "approved"
        assert ApprovalStatus.REJECTED == "rejected"
        assert ApprovalStatus.EXPIRED == "expired"

    def test_approval_timestamps_frozen(self) -> None:
        from datetime import UTC, datetime

        ts = ApprovalTimestamps(created_at=datetime.now(UTC))
        with pytest.raises(AttributeError):
            ts.created_at = datetime.now(UTC)  # type: ignore[misc]

    def test_expiry_policy_defaults(self) -> None:
        from datetime import UTC, datetime

        policy = ExpiryPolicy(expires_at=datetime.now(UTC))
        assert policy.duration_hours == 24.0

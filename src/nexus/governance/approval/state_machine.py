"""Generic state machine with transition validation.

Issue #1359 Phase 0: Extracted from DisputeService.VALID_TRANSITIONS pattern.
Reusable for any workflow with enumerated states and valid transitions.
"""

from __future__ import annotations

from typing import Any


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed."""

    def __init__(self, current: str, target: str, valid: frozenset[str]) -> None:
        self.current = current
        self.target = target
        self.valid = valid
        super().__init__(
            f"Cannot transition from {current!r} to {target!r}. "
            f"Valid targets: {sorted(valid) if valid else 'none (terminal state)'}"
        )


class StateMachine:
    """Generic state machine with immutable transition rules.

    Usage:
        sm = StateMachine({
            "pending": frozenset({"approved", "rejected"}),
            "approved": frozenset(),
            "rejected": frozenset(),
        })
        new_state = sm.transition("pending", "approved")  # "approved"
        sm.transition("approved", "pending")  # raises InvalidTransitionError
    """

    def __init__(self, transitions: dict[str, frozenset[str]]) -> None:
        # Validate: all target states must be defined as source states
        all_targets = frozenset().union(*transitions.values())
        undefined = all_targets - transitions.keys()
        if undefined:
            msg = f"Target states {sorted(undefined)} not defined in transitions"
            raise ValueError(msg)

        self._transitions: dict[str, frozenset[str]] = {
            k: frozenset(v) for k, v in transitions.items()
        }

    @property
    def states(self) -> frozenset[str]:
        """All known states."""
        return frozenset(self._transitions.keys())

    @property
    def terminal_states(self) -> frozenset[str]:
        """States with no outgoing transitions."""
        return frozenset(s for s, targets in self._transitions.items() if not targets)

    def valid_targets(self, current: str) -> frozenset[str]:
        """Get valid target states from current state.

        Raises:
            KeyError: If current state is not defined.
        """
        if current not in self._transitions:
            msg = f"Unknown state: {current!r}"
            raise KeyError(msg)
        return self._transitions[current]

    def is_terminal(self, state: str) -> bool:
        """Check if a state is terminal (no outgoing transitions)."""
        return state in self.terminal_states

    def transition(self, current: str, target: str) -> str:
        """Validate and return the new state.

        Returns:
            The target state if the transition is valid.

        Raises:
            KeyError: If current state is not defined.
            InvalidTransitionError: If the transition is not allowed.
        """
        valid = self.valid_targets(current)
        if target not in valid:
            raise InvalidTransitionError(current, target, valid)
        return target

    def to_dict(self) -> dict[str, list[str]]:
        """Serialize transitions for debugging/API responses."""
        return {k: sorted(v) for k, v in self._transitions.items()}

    def __repr__(self) -> str:
        return (
            f"StateMachine(states={sorted(self.states)}, terminal={sorted(self.terminal_states)})"
        )

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, StateMachine):
            return NotImplemented
        return self._transitions == other._transitions

    def __hash__(self) -> int:
        return hash(tuple(sorted((k, v) for k, v in self._transitions.items())))

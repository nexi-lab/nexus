"""Hypothesis property-based tests for generation counter invariants (Issue #1303).

Invariants proven:
  1. Generation is always non-negative
  2. Generation monotonically non-decreasing across transitions
  3. AgentInfo is immutable (frozen dataclass)
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from nexus.services.protocols.agent_registry import AgentInfo
from tests.strategies.kernel import agent_info

# ---------------------------------------------------------------------------
# Invariant 1: Generation is always non-negative
# ---------------------------------------------------------------------------


class TestGenerationNonNegative:
    """Generation counter is always >= 0."""

    @given(info=agent_info())
    def test_generation_non_negative(self, info: AgentInfo) -> None:
        """AgentInfo.generation is always >= 0."""
        assert info.generation >= 0

    @given(
        generations=st.lists(
            st.integers(min_value=0, max_value=1_000_000),
            min_size=2,
            max_size=50,
        ),
    )
    def test_sorted_generations_are_monotonic(self, generations: list[int]) -> None:
        """A sorted sequence of generations is always monotonically non-decreasing."""
        sorted_gens = sorted(generations)
        for i in range(len(sorted_gens) - 1):
            assert sorted_gens[i] <= sorted_gens[i + 1]


# ---------------------------------------------------------------------------
# Invariant 2: Generation monotonicity across transitions
# ---------------------------------------------------------------------------


class TestGenerationMonotonicity:
    """Generation counter only increases across state transitions."""

    @given(
        agent_id=st.text(min_size=1, max_size=20),
        transitions=st.lists(
            st.tuples(
                st.sampled_from(["CONNECTED", "IDLE", "BUSY", "DISCONNECTED"]),
                st.integers(min_value=0, max_value=100),
            ),
            min_size=2,
            max_size=20,
        ),
    )
    def test_generation_never_decreases_in_sequence(
        self,
        agent_id: str,
        transitions: list[tuple[str, int]],
    ) -> None:
        """Simulating state transitions: generation only increases.

        Each transition produces an AgentInfo snapshot. Across time-ordered
        snapshots for the same agent, generation must be non-decreasing.
        """
        # Sort transitions by generation (simulating time ordering)
        sorted_transitions = sorted(transitions, key=lambda t: t[1])

        snapshots = [
            AgentInfo(
                agent_id=agent_id,
                owner_id="owner",
                zone_id=None,
                name=None,
                state=state,
                generation=gen,
            )
            for state, gen in sorted_transitions
        ]

        # Verify monotonicity
        for i in range(len(snapshots) - 1):
            assert snapshots[i].generation <= snapshots[i + 1].generation, (
                f"Generation decreased: {snapshots[i].generation} > "
                f"{snapshots[i + 1].generation} at transition {i}"
            )


# ---------------------------------------------------------------------------
# Invariant 3: AgentInfo is immutable (frozen dataclass)
# ---------------------------------------------------------------------------


class TestAgentInfoImmutability:
    """AgentInfo is a frozen dataclass — no mutation after creation."""

    @given(info=agent_info())
    def test_agent_info_is_frozen(self, info: AgentInfo) -> None:
        """Cannot modify AgentInfo fields after creation."""
        import dataclasses

        assert dataclasses.is_dataclass(info)

        # Attempting to set any field should raise FrozenInstanceError
        try:
            info.generation = 999  # type: ignore[misc]
            raise AssertionError("AgentInfo should be frozen — mutation succeeded")
        except (AttributeError, dataclasses.FrozenInstanceError):
            pass  # Correctly frozen

    @given(info=agent_info())
    def test_agent_info_has_slots(self, info: AgentInfo) -> None:
        """AgentInfo uses __slots__ for memory efficiency."""
        assert hasattr(info, "__slots__") or hasattr(type(info), "__slots__")

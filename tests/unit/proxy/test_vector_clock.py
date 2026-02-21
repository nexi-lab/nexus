"""Unit tests for VectorClock — causal ordering for edge split-brain.

Issue #1707: Edge split-brain resilience.
"""

from __future__ import annotations

import pytest

from nexus.proxy.vector_clock import CausalOrder, VectorClock


class TestVectorClockIncrement:
    """increment() creates a new clock with the given node's counter bumped."""

    def test_increment_new_node(self) -> None:
        vc = VectorClock()
        result = vc.increment("edge-1")
        assert result.counters == {"edge-1": 1}
        # Original is unchanged (immutable)
        assert vc.counters == {}

    def test_increment_existing_node(self) -> None:
        vc = VectorClock(counters={"edge-1": 3})
        result = vc.increment("edge-1")
        assert result.counters == {"edge-1": 4}

    def test_increment_preserves_other_nodes(self) -> None:
        vc = VectorClock(counters={"edge-1": 2, "cloud": 5})
        result = vc.increment("edge-1")
        assert result.counters == {"edge-1": 3, "cloud": 5}


class TestVectorClockMerge:
    """merge() returns pointwise max of two clocks."""

    def test_merge_disjoint_nodes(self) -> None:
        a = VectorClock(counters={"edge-1": 3})
        b = VectorClock(counters={"cloud": 5})
        merged = a.merge(b)
        assert merged.counters == {"edge-1": 3, "cloud": 5}

    def test_merge_overlapping_nodes(self) -> None:
        a = VectorClock(counters={"edge-1": 3, "cloud": 2})
        b = VectorClock(counters={"edge-1": 1, "cloud": 5})
        merged = a.merge(b)
        assert merged.counters == {"edge-1": 3, "cloud": 5}

    def test_merge_with_empty(self) -> None:
        a = VectorClock(counters={"edge-1": 3})
        b = VectorClock()
        assert a.merge(b).counters == {"edge-1": 3}
        assert b.merge(a).counters == {"edge-1": 3}

    def test_merge_is_commutative(self) -> None:
        a = VectorClock(counters={"edge-1": 3, "cloud": 2})
        b = VectorClock(counters={"edge-1": 1, "cloud": 5})
        assert a.merge(b).counters == b.merge(a).counters


class TestVectorClockCompare:
    """compare() determines causal ordering between two clocks."""

    def test_equal_clocks(self) -> None:
        a = VectorClock(counters={"edge-1": 3, "cloud": 2})
        b = VectorClock(counters={"edge-1": 3, "cloud": 2})
        assert a.compare(b) is CausalOrder.EQUAL

    def test_empty_clocks_are_equal(self) -> None:
        assert VectorClock().compare(VectorClock()) is CausalOrder.EQUAL

    def test_before(self) -> None:
        a = VectorClock(counters={"edge-1": 1, "cloud": 2})
        b = VectorClock(counters={"edge-1": 2, "cloud": 3})
        assert a.compare(b) is CausalOrder.BEFORE

    def test_after(self) -> None:
        a = VectorClock(counters={"edge-1": 5, "cloud": 3})
        b = VectorClock(counters={"edge-1": 2, "cloud": 1})
        assert a.compare(b) is CausalOrder.AFTER

    def test_concurrent(self) -> None:
        a = VectorClock(counters={"edge-1": 3, "cloud": 1})
        b = VectorClock(counters={"edge-1": 1, "cloud": 3})
        assert a.compare(b) is CausalOrder.CONCURRENT

    def test_before_with_missing_node(self) -> None:
        """Clock with node missing (=0) is before one with that node present."""
        a = VectorClock(counters={"edge-1": 1})
        b = VectorClock(counters={"edge-1": 1, "cloud": 1})
        assert a.compare(b) is CausalOrder.BEFORE

    def test_after_with_extra_node(self) -> None:
        a = VectorClock(counters={"edge-1": 1, "cloud": 1})
        b = VectorClock(counters={"edge-1": 1})
        assert a.compare(b) is CausalOrder.AFTER

    def test_concurrent_different_nodes_advanced(self) -> None:
        """Each advanced a different node — true concurrency."""
        a = VectorClock(counters={"edge-1": 2, "cloud": 1})
        b = VectorClock(counters={"edge-1": 1, "cloud": 2})
        assert a.compare(b) is CausalOrder.CONCURRENT


class TestVectorClockSerialization:
    """to_json() / from_json() round-trip."""

    def test_round_trip(self) -> None:
        original = VectorClock(counters={"edge-1": 3, "cloud": 7})
        json_str = original.to_json()
        restored = VectorClock.from_json(json_str)
        assert restored.counters == original.counters

    def test_empty_round_trip(self) -> None:
        original = VectorClock()
        restored = VectorClock.from_json(original.to_json())
        assert restored.counters == {}

    def test_from_json_invalid_type(self) -> None:
        with pytest.raises(ValueError, match="Expected dict"):
            VectorClock.from_json('"not a dict"')

    def test_to_json_sorted_keys(self) -> None:
        vc = VectorClock(counters={"z-node": 1, "a-node": 2})
        json_str = vc.to_json()
        assert json_str == '{"a-node": 2, "z-node": 1}'


class TestVectorClockBool:
    """__bool__ returns False for empty, True otherwise."""

    def test_empty_is_falsy(self) -> None:
        assert not VectorClock()

    def test_non_empty_is_truthy(self) -> None:
        assert VectorClock(counters={"a": 1})


class TestVectorClockImmutability:
    """VectorClock is frozen — mutation raises TypeError."""

    def test_frozen(self) -> None:
        vc = VectorClock(counters={"a": 1})
        with pytest.raises(AttributeError):
            vc.counters = {"b": 2}

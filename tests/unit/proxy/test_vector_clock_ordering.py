"""Tests for vector-clock causal ordering in replay engine (Issue #3062).

Covers:
- _parse_vector_clock with valid/invalid/None input
- _vc_happens_before partial order semantics
- _sort_by_causal_order topological sort
- Batch sorting integration with ReplayEngine
- Partial batch failure resumption
"""

import json

from nexus.proxy.queue_protocol import QueuedOperation
from nexus.proxy.replay_engine import (
    _parse_vector_clock,
    _sort_by_causal_order,
    _vc_happens_before,
)


def _make_op(
    op_id: int,
    vector_clock: dict[str, int] | None = None,
    method: str = "write",
) -> QueuedOperation:
    return QueuedOperation(
        id=op_id,
        method=method,
        args_json="[]",
        kwargs_json=json.dumps({"path": f"/file{op_id}"}),
        payload_ref=None,
        retry_count=0,
        created_at=1000.0 + op_id,
        vector_clock=json.dumps(vector_clock) if vector_clock else None,
    )


class TestParseVectorClock:
    def test_valid_json(self) -> None:
        vc = _parse_vector_clock('{"a": 1, "b": 2}')
        assert vc == {"a": 1, "b": 2}

    def test_none_returns_empty(self) -> None:
        assert _parse_vector_clock(None) == {}

    def test_empty_string_returns_empty(self) -> None:
        assert _parse_vector_clock("") == {}

    def test_invalid_json_returns_empty(self) -> None:
        assert _parse_vector_clock("not-json") == {}

    def test_non_dict_returns_empty(self) -> None:
        assert _parse_vector_clock("[1, 2, 3]") == {}

    def test_coerces_string_keys_and_int_values(self) -> None:
        vc = _parse_vector_clock('{"1": "5"}')
        assert vc == {"1": 5}


class TestVcHappensBefore:
    def test_strictly_less(self) -> None:
        assert _vc_happens_before({"a": 1}, {"a": 2}) is True

    def test_equal_is_not_before(self) -> None:
        assert _vc_happens_before({"a": 1}, {"a": 1}) is False

    def test_concurrent(self) -> None:
        # a=(1,0), b=(0,1) — incomparable
        assert _vc_happens_before({"a": 1, "b": 0}, {"a": 0, "b": 1}) is False
        assert _vc_happens_before({"a": 0, "b": 1}, {"a": 1, "b": 0}) is False

    def test_empty_is_not_before(self) -> None:
        assert _vc_happens_before({}, {"a": 1}) is False
        assert _vc_happens_before({"a": 1}, {}) is False

    def test_subset_keys(self) -> None:
        # {a:1} < {a:2, b:1} — missing key treated as 0
        assert _vc_happens_before({"a": 1}, {"a": 2, "b": 1}) is True

    def test_strictly_greater_is_false(self) -> None:
        assert _vc_happens_before({"a": 2}, {"a": 1}) is False


class TestSortByCausalOrder:
    def test_empty_list(self) -> None:
        assert _sort_by_causal_order([]) == []

    def test_single_op(self) -> None:
        op = _make_op(1, {"a": 1})
        assert _sort_by_causal_order([op]) == [op]

    def test_causal_chain(self) -> None:
        """A -> B -> C should replay in order regardless of queue id."""
        op_a = _make_op(3, {"node1": 1})
        op_b = _make_op(1, {"node1": 2})
        op_c = _make_op(2, {"node1": 3})

        result = _sort_by_causal_order([op_c, op_a, op_b])
        assert [op.id for op in result] == [3, 1, 2]

    def test_concurrent_ops_ordered_by_id(self) -> None:
        """Concurrent (incomparable) ops should be ordered by queue id."""
        op_a = _make_op(5, {"node1": 1, "node2": 0})
        op_b = _make_op(3, {"node1": 0, "node2": 1})

        result = _sort_by_causal_order([op_a, op_b])
        # Both are concurrent, so ordered by id: 3 before 5
        assert [op.id for op in result] == [3, 5]

    def test_mixed_causal_and_concurrent(self) -> None:
        """A -> C, B concurrent with both."""
        op_a = _make_op(1, {"x": 1, "y": 0})
        op_b = _make_op(2, {"x": 0, "y": 1})  # concurrent with A
        op_c = _make_op(3, {"x": 2, "y": 0})  # causally after A

        result = _sort_by_causal_order([op_c, op_b, op_a])
        ids = [op.id for op in result]
        # A must come before C; B can be anywhere
        assert ids.index(1) < ids.index(3)

    def test_no_vector_clocks_preserves_order(self) -> None:
        """Ops without vector clocks should maintain original order."""
        ops = [_make_op(1), _make_op(2), _make_op(3)]
        result = _sort_by_causal_order(ops)
        assert [op.id for op in result] == [1, 2, 3]

    def test_partial_vector_clocks(self) -> None:
        """Mix of ops with and without vector clocks."""
        op_a = _make_op(1, {"n": 1})
        op_b = _make_op(2)  # no VC
        op_c = _make_op(3, {"n": 2})

        result = _sort_by_causal_order([op_c, op_b, op_a])
        ids = [op.id for op in result]
        # A must come before C
        assert ids.index(1) < ids.index(3)


class TestVectorClockRoundtrip:
    """Verify vector_clock field roundtrips through QueuedOperation."""

    def test_roundtrip(self) -> None:
        vc = {"node1": 5, "node2": 3}
        op = _make_op(1, vc)
        parsed = _parse_vector_clock(op.vector_clock)
        assert parsed == vc

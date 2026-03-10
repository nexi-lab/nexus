"""Rust RingBufferCore integration tests (nexus_fast.RingBufferCore).

Tests the Rust data-plane directly, bypassing the Python RingBuffer wrapper.
"""

import pytest
from nexus_fast import RingBufferCore


class TestRingBufferCoreBasic:
    def test_create(self) -> None:
        core = RingBufferCore(1024)
        assert core.capacity == 1024
        assert core.size == 0
        assert core.msg_count == 0
        assert core.closed is False

    def test_zero_capacity_rejected(self) -> None:
        with pytest.raises(ValueError, match="capacity must be > 0"):
            RingBufferCore(0)

    def test_push_pop_roundtrip(self) -> None:
        core = RingBufferCore(1024)
        n = core.push(b"hello")
        assert n == 5
        assert core.size == 5
        assert core.msg_count == 1
        result = core.pop()
        assert result == b"hello"
        assert core.size == 0
        assert core.msg_count == 0

    def test_fifo_ordering(self) -> None:
        core = RingBufferCore(1024)
        core.push(b"first")
        core.push(b"second")
        core.push(b"third")
        assert core.pop() == b"first"
        assert core.pop() == b"second"
        assert core.pop() == b"third"

    def test_empty_push_is_noop(self) -> None:
        core = RingBufferCore(1024)
        assert core.push(b"") == 0
        assert core.msg_count == 0

    def test_peek(self) -> None:
        core = RingBufferCore(1024)
        assert core.peek() is None
        core.push(b"msg")
        assert core.peek() == b"msg"
        assert core.msg_count == 1  # not consumed

    def test_peek_all(self) -> None:
        core = RingBufferCore(1024)
        core.push(b"a")
        core.push(b"b")
        result = core.peek_all()
        assert list(result) == [b"a", b"b"]
        assert core.msg_count == 2


class TestRingBufferCoreCapacity:
    def test_exact_capacity(self) -> None:
        core = RingBufferCore(10)
        core.push(b"x" * 10)
        assert core.is_full()

    def test_oversized_rejected(self) -> None:
        core = RingBufferCore(10)
        with pytest.raises(ValueError, match="exceeds buffer capacity"):
            core.push(b"x" * 11)

    def test_full_raises(self) -> None:
        core = RingBufferCore(10)
        core.push(b"x" * 10)
        with pytest.raises(RuntimeError, match="PipeFull"):
            core.push(b"y")

    def test_space_freed_after_pop(self) -> None:
        core = RingBufferCore(20)
        core.push(b"x" * 15)
        core.pop()
        core.push(b"y" * 20)
        assert core.size == 20


class TestRingBufferCoreClose:
    def test_close(self) -> None:
        core = RingBufferCore(1024)
        assert not core.closed
        core.close()
        assert core.closed

    def test_push_after_close_raises(self) -> None:
        core = RingBufferCore(1024)
        core.close()
        with pytest.raises(RuntimeError, match="PipeClosed"):
            core.push(b"data")

    def test_pop_closed_empty_raises(self) -> None:
        core = RingBufferCore(1024)
        core.close()
        with pytest.raises(RuntimeError, match="PipeClosed"):
            core.pop()

    def test_pop_drains_before_closed_error(self) -> None:
        core = RingBufferCore(1024)
        core.push(b"last")
        core.close()
        assert core.pop() == b"last"
        with pytest.raises(RuntimeError, match="PipeClosed"):
            core.pop()

    def test_pop_empty_raises(self) -> None:
        core = RingBufferCore(1024)
        with pytest.raises(RuntimeError, match="PipeEmpty"):
            core.pop()


class TestRingBufferCoreStats:
    def test_stats_dict(self) -> None:
        core = RingBufferCore(256)
        s = core.stats()
        assert s["size"] == 0
        assert s["capacity"] == 256
        assert s["msg_count"] == 0
        assert s["closed"] is False
        assert s["push_count"] == 0
        assert s["pop_count"] == 0

    def test_stats_after_ops(self) -> None:
        core = RingBufferCore(1024)
        core.push(b"hello")
        core.push(b"world")
        core.pop()
        s = core.stats()
        assert s["push_count"] == 2
        assert s["pop_count"] == 1
        assert s["msg_count"] == 1
        assert s["size"] == 5  # "world"

    def test_is_empty_is_full(self) -> None:
        core = RingBufferCore(5)
        assert core.is_empty()
        assert not core.is_full()
        core.push(b"12345")
        assert not core.is_empty()
        assert core.is_full()

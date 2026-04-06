"""Rust RingBufferCore integration tests (nexus_kernel.RingBufferCore).

Tests the Rust data-plane directly, bypassing the Python MemoryPipeBackend wrapper.
"""

import pytest
from nexus_kernel import RingBufferCore


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


class TestRingBufferCoreWrapAround:
    def test_wrap_around_basic(self) -> None:
        """Fill/drain cycles force sentinel wrap-around in the byte ring."""
        core = RingBufferCore(64)
        for cycle in range(10):
            msg = f"cycle-{cycle}".encode()
            core.push(msg)
            assert core.pop() == msg

    def test_wrap_around_large_messages(self) -> None:
        """Large messages near ring end trigger sentinel + wrap."""
        core = RingBufferCore(64)
        for val in [0xAA, 0xBB, 0xCC, 0xDD]:
            core.push(bytes([val]) * 50)
            assert core.pop() == bytes([val]) * 50

    def test_wrap_around_many_small(self) -> None:
        """100 single-byte push/pop cycles exercise many wrap-arounds."""
        core = RingBufferCore(32)
        for i in range(100):
            core.push(bytes([i % 256]))
            assert core.pop() == bytes([i % 256])


class TestRingBufferCoreU64:
    def test_push_u64_pop_u64_roundtrip(self) -> None:
        core = RingBufferCore(1024)
        core.push_u64(42)
        core.push_u64(2**64 - 1)
        core.push_u64(0)
        assert core.pop_u64() == 42
        assert core.pop_u64() == 2**64 - 1
        assert core.pop_u64() == 0

    def test_u64_size_tracking(self) -> None:
        core = RingBufferCore(1024)
        core.push_u64(99)
        assert core.size == 8
        assert core.msg_count == 1
        core.pop_u64()
        assert core.size == 0
        assert core.msg_count == 0

    def test_interleaved_bytes_u64(self) -> None:
        """Mix push/push_u64 in same ring."""
        core = RingBufferCore(1024)
        core.push(b"hello")
        core.push_u64(12345)
        core.push(b"world")
        assert core.pop() == b"hello"
        assert core.pop_u64() == 12345
        assert core.pop() == b"world"

    def test_pop_u64_wrong_size(self) -> None:
        """push 5-byte msg, pop_u64 → ValueError."""
        core = RingBufferCore(1024)
        core.push(b"12345")  # 5 bytes, not 8
        with pytest.raises(ValueError, match="pop_u64 expects 8-byte payload, got 5"):
            core.pop_u64()

    def test_u64_wrap_around(self) -> None:
        """u64 messages wrapping around the ring."""
        core = RingBufferCore(32)
        for i in range(50):
            core.push_u64(i)
            assert core.pop_u64() == i

    def test_u64_closed_raises(self) -> None:
        core = RingBufferCore(1024)
        core.close()
        with pytest.raises(RuntimeError, match="PipeClosed"):
            core.push_u64(42)
        with pytest.raises(RuntimeError, match="PipeClosed"):
            core.pop_u64()

    def test_u64_empty_raises(self) -> None:
        core = RingBufferCore(1024)
        with pytest.raises(RuntimeError, match="PipeEmpty"):
            core.pop_u64()

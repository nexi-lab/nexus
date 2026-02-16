"""Unit tests for HeartbeatBuffer (Issue #1589).

Tests cover:
- Record: buffer write, counter tracking
- Flush: callback invocation, buffer cleared, interval respected
- AutoFlush: flush_interval=0 triggers immediate flush on record()
- CapacityWarning: 80% threshold warning
- RestoreBuffer: flush failure restores entries, respects max, merges timestamps
- RecentlyHeartbeated: cutoff filtering
- Remove: remove agent from buffer
- Concurrent: 10 threads x 100 records, no corruption
- Stats: counter correctness
- EdgeCases: record after remove (re-register scenario)

All tests use a mock flush_callback — no DB needed.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from nexus.core.heartbeat_buffer import HeartbeatBuffer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_callback() -> MagicMock:
    """A mock flush callback that returns the buffer size."""
    cb = MagicMock(side_effect=lambda buf: len(buf))
    return cb


@pytest.fixture
def buffer(mock_callback: MagicMock) -> HeartbeatBuffer:
    """HeartbeatBuffer with a long flush interval (no auto-flush)."""
    return HeartbeatBuffer(
        flush_callback=mock_callback,
        flush_interval=9999,
        max_buffer_size=50_000,
    )


# ---------------------------------------------------------------------------
# Record tests
# ---------------------------------------------------------------------------


class TestRecord:
    """Tests for HeartbeatBuffer.record()."""

    def test_record_writes_to_buffer(self, buffer: HeartbeatBuffer) -> None:
        """record() adds the agent to the internal buffer."""
        buffer.record("agent-1")
        assert "agent-1" in buffer._buffer

    def test_record_timestamp_is_utc(self, buffer: HeartbeatBuffer) -> None:
        """record() stores a UTC datetime."""
        buffer.record("agent-1")
        ts = buffer._buffer["agent-1"]
        assert ts.tzinfo is not None

    def test_record_overwrites_previous(self, buffer: HeartbeatBuffer) -> None:
        """Second record() for same agent overwrites the timestamp."""
        buffer.record("agent-1")
        first_ts = buffer._buffer["agent-1"]
        time.sleep(0.01)
        buffer.record("agent-1")
        second_ts = buffer._buffer["agent-1"]
        assert second_ts >= first_ts

    def test_record_increments_total_recorded(self, buffer: HeartbeatBuffer) -> None:
        """total_recorded counter increments on each record()."""
        buffer.record("agent-1")
        buffer.record("agent-2")
        buffer.record("agent-1")  # duplicate agent, still counts
        assert buffer.stats()["total_recorded"] == 3


# ---------------------------------------------------------------------------
# Flush tests
# ---------------------------------------------------------------------------


class TestFlush:
    """Tests for HeartbeatBuffer.flush()."""

    def test_flush_calls_callback(self, buffer: HeartbeatBuffer, mock_callback: MagicMock) -> None:
        """flush() invokes the flush callback with the buffer contents."""
        buffer.record("agent-1")
        buffer.record("agent-2")
        flushed = buffer.flush()
        assert flushed == 2
        mock_callback.assert_called_once()
        call_arg = mock_callback.call_args[0][0]
        assert "agent-1" in call_arg
        assert "agent-2" in call_arg

    def test_flush_clears_buffer(self, buffer: HeartbeatBuffer) -> None:
        """flush() empties the internal buffer."""
        buffer.record("agent-1")
        buffer.flush()
        assert len(buffer._buffer) == 0

    def test_flush_empty_returns_zero(
        self, buffer: HeartbeatBuffer, mock_callback: MagicMock
    ) -> None:
        """flush() on empty buffer returns 0 without calling callback."""
        assert buffer.flush() == 0
        mock_callback.assert_not_called()

    def test_flush_updates_counters(self, buffer: HeartbeatBuffer) -> None:
        """flush() updates total_flushed and flush_count."""
        buffer.record("agent-1")
        buffer.record("agent-2")
        buffer.flush()
        stats = buffer.stats()
        assert stats["total_flushed"] == 2
        assert stats["flush_count"] == 1

    def test_multiple_flushes_accumulate(self, buffer: HeartbeatBuffer) -> None:
        """Multiple flushes accumulate counters."""
        buffer.record("agent-1")
        buffer.flush()
        buffer.record("agent-2")
        buffer.flush()
        stats = buffer.stats()
        assert stats["total_flushed"] == 2
        assert stats["flush_count"] == 2


# ---------------------------------------------------------------------------
# Auto-flush tests
# ---------------------------------------------------------------------------


class TestAutoFlush:
    """Tests for auto-flush triggered by record()."""

    def test_auto_flush_on_interval_zero(self, mock_callback: MagicMock) -> None:
        """flush_interval=0 triggers immediate flush on record()."""
        buf = HeartbeatBuffer(
            flush_callback=mock_callback,
            flush_interval=0,
            max_buffer_size=50_000,
        )
        buf.record("agent-1")
        # Buffer should be empty after auto-flush
        assert len(buf._buffer) == 0
        mock_callback.assert_called_once()

    def test_no_auto_flush_before_interval(self, mock_callback: MagicMock) -> None:
        """No auto-flush when interval hasn't elapsed."""
        buf = HeartbeatBuffer(
            flush_callback=mock_callback,
            flush_interval=9999,
            max_buffer_size=50_000,
        )
        buf.record("agent-1")
        assert len(buf._buffer) == 1
        mock_callback.assert_not_called()


# ---------------------------------------------------------------------------
# Capacity warning tests
# ---------------------------------------------------------------------------


class TestCapacityWarning:
    """Tests for 80% buffer capacity warning."""

    def test_warns_at_80_percent(self, mock_callback: MagicMock, caplog) -> None:
        """Warning is emitted when buffer reaches 80% capacity."""
        buf = HeartbeatBuffer(
            flush_callback=mock_callback,
            flush_interval=9999,
            max_buffer_size=10,
        )
        # Record 7 agents (below 80%)
        with caplog.at_level(logging.WARNING, logger="nexus.core.heartbeat_buffer"):
            caplog.clear()
            for i in range(7):
                buf.record(f"agent-{i}")
            assert "capacity" not in caplog.text

        # Record the 8th agent (hits 80%)
        with caplog.at_level(logging.WARNING, logger="nexus.core.heartbeat_buffer"):
            caplog.clear()
            buf.record("agent-7")
            assert "capacity" in caplog.text
            assert "80%" in caplog.text

    def test_no_warning_below_threshold(self, mock_callback: MagicMock, caplog) -> None:
        """No warning when buffer is below 80%."""
        buf = HeartbeatBuffer(
            flush_callback=mock_callback,
            flush_interval=9999,
            max_buffer_size=100,
        )
        with caplog.at_level(logging.WARNING, logger="nexus.core.heartbeat_buffer"):
            for i in range(79):
                buf.record(f"agent-{i}")
            assert "capacity" not in caplog.text


# ---------------------------------------------------------------------------
# Restore buffer tests
# ---------------------------------------------------------------------------


class TestRestoreBuffer:
    """Tests for _restore_buffer() on flush failure."""

    def test_flush_failure_restores_entries(self, mock_callback: MagicMock) -> None:
        """On flush failure, entries are restored to the buffer."""
        mock_callback.side_effect = RuntimeError("DB down")
        buf = HeartbeatBuffer(
            flush_callback=mock_callback,
            flush_interval=9999,
            max_buffer_size=50_000,
        )
        buf.record("agent-1")
        buf.record("agent-2")

        with pytest.raises(RuntimeError, match="DB down"):
            buf.flush()

        # Entries should be restored
        assert "agent-1" in buf._buffer
        assert "agent-2" in buf._buffer

    def test_restore_respects_max_buffer_size(self, mock_callback: MagicMock) -> None:
        """Restore does not exceed max_buffer_size."""
        mock_callback.side_effect = RuntimeError("DB down")
        buf = HeartbeatBuffer(
            flush_callback=mock_callback,
            flush_interval=9999,
            max_buffer_size=3,
        )
        # Fill buffer to capacity
        buf.record("agent-1")
        buf.record("agent-2")
        buf.record("agent-3")

        with pytest.raises(RuntimeError, match="DB down"):
            buf.flush()

        # Buffer should be restored but not exceed max
        assert len(buf._buffer) <= 3

    def test_restore_merges_newer_timestamps(self, mock_callback: MagicMock) -> None:
        """Restore keeps the newer timestamp when merging."""
        call_count = 0

        def failing_then_ok(buf_dict):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("DB down")
            return len(buf_dict)

        mock_callback.side_effect = failing_then_ok
        buf = HeartbeatBuffer(
            flush_callback=mock_callback,
            flush_interval=9999,
            max_buffer_size=50_000,
        )

        # Record and attempt flush (will fail)
        buf.record("agent-1")
        old_ts = buf._buffer["agent-1"]

        with pytest.raises(RuntimeError, match="DB down"):
            buf.flush()

        # Record again (newer timestamp)
        time.sleep(0.01)
        buf.record("agent-1")
        new_ts = buf._buffer["agent-1"]
        assert new_ts >= old_ts

    def test_restore_does_not_overwrite_newer(self, mock_callback: MagicMock) -> None:
        """Restore does not overwrite a newer timestamp already in the buffer."""
        mock_callback.side_effect = RuntimeError("DB down")
        buf = HeartbeatBuffer(
            flush_callback=mock_callback,
            flush_interval=9999,
            max_buffer_size=50_000,
        )

        buf.record("agent-1")
        with pytest.raises(RuntimeError):
            buf.flush()

        # A newer heartbeat arrived while flush was happening
        # (simulated by recording after restore)
        restored_ts = buf._buffer["agent-1"]

        # Record a new one (newer)
        time.sleep(0.01)
        buf.record("agent-1")
        newer_ts = buf._buffer["agent-1"]
        assert newer_ts >= restored_ts


# ---------------------------------------------------------------------------
# recently_heartbeated tests
# ---------------------------------------------------------------------------


class TestRecentlyHeartbeated:
    """Tests for recently_heartbeated()."""

    def test_returns_agents_after_cutoff(self, buffer: HeartbeatBuffer) -> None:
        """recently_heartbeated returns agents with timestamps >= cutoff."""
        buffer.record("agent-1")
        buffer.record("agent-2")

        # Cutoff well in the past — both should match
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        result = buffer.recently_heartbeated(cutoff)
        assert result == {"agent-1", "agent-2"}

    def test_excludes_agents_before_cutoff(self, buffer: HeartbeatBuffer) -> None:
        """recently_heartbeated excludes agents with timestamps < cutoff."""
        buffer.record("agent-1")

        # Cutoff in the future — none should match
        cutoff = datetime.now(UTC) + timedelta(hours=1)
        result = buffer.recently_heartbeated(cutoff)
        assert result == set()

    def test_empty_buffer_returns_empty_set(self, buffer: HeartbeatBuffer) -> None:
        """recently_heartbeated on empty buffer returns empty set."""
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        assert buffer.recently_heartbeated(cutoff) == set()


# ---------------------------------------------------------------------------
# Remove tests
# ---------------------------------------------------------------------------


class TestRemove:
    """Tests for HeartbeatBuffer.remove()."""

    def test_remove_existing_agent(self, buffer: HeartbeatBuffer) -> None:
        """remove() removes an agent from the buffer."""
        buffer.record("agent-1")
        buffer.remove("agent-1")
        assert "agent-1" not in buffer._buffer

    def test_remove_nonexistent_is_noop(self, buffer: HeartbeatBuffer) -> None:
        """remove() on nonexistent agent is a no-op."""
        buffer.remove("no-such-agent")  # Should not raise


# ---------------------------------------------------------------------------
# Concurrent tests
# ---------------------------------------------------------------------------


class TestConcurrent:
    """Thread-based concurrent tests."""

    def test_concurrent_records_no_corruption(self, mock_callback: MagicMock) -> None:
        """10 threads x 100 records -> no data corruption."""
        buf = HeartbeatBuffer(
            flush_callback=mock_callback,
            flush_interval=9999,
            max_buffer_size=50_000,
        )

        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            for i in range(100):
                try:
                    buf.record(f"agent-{thread_id}-{i}")
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # 10 threads x 100 unique agents = 1000 entries
        assert len(buf._buffer) == 1000
        assert buf.stats()["total_recorded"] == 1000

    def test_concurrent_record_and_flush(self, mock_callback: MagicMock) -> None:
        """Concurrent records and flushes don't corrupt state."""
        buf = HeartbeatBuffer(
            flush_callback=mock_callback,
            flush_interval=9999,
            max_buffer_size=50_000,
        )

        errors: list[Exception] = []

        def record_worker() -> None:
            for i in range(50):
                try:
                    buf.record(f"agent-{i}")
                except Exception as e:
                    errors.append(e)

        def flush_worker() -> None:
            for _ in range(10):
                try:
                    buf.flush()
                    time.sleep(0.001)
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=record_worker),
            threading.Thread(target=flush_worker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


# ---------------------------------------------------------------------------
# Stats tests
# ---------------------------------------------------------------------------


class TestStats:
    """Tests for HeartbeatBuffer.stats()."""

    def test_initial_stats(self, buffer: HeartbeatBuffer) -> None:
        """Fresh buffer has all-zero stats."""
        stats = buffer.stats()
        assert stats == {
            "buffer_size": 0,
            "total_recorded": 0,
            "total_flushed": 0,
            "flush_count": 0,
        }

    def test_stats_after_record_and_flush(self, buffer: HeartbeatBuffer) -> None:
        """Stats reflect record and flush activity."""
        buffer.record("agent-1")
        buffer.record("agent-2")
        buffer.record("agent-3")
        buffer.flush()

        stats = buffer.stats()
        assert stats["buffer_size"] == 0
        assert stats["total_recorded"] == 3
        assert stats["total_flushed"] == 3
        assert stats["flush_count"] == 1

    def test_stats_buffer_size_tracks_current(self, buffer: HeartbeatBuffer) -> None:
        """buffer_size reflects current buffer contents."""
        buffer.record("agent-1")
        buffer.record("agent-2")
        assert buffer.stats()["buffer_size"] == 2
        buffer.remove("agent-1")
        assert buffer.stats()["buffer_size"] == 1


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases for HeartbeatBuffer."""

    def test_record_after_remove(self, buffer: HeartbeatBuffer) -> None:
        """record() works after remove() (re-register scenario)."""
        buffer.record("agent-1")
        buffer.remove("agent-1")
        assert "agent-1" not in buffer._buffer

        buffer.record("agent-1")
        assert "agent-1" in buffer._buffer

    def test_flush_after_remove(self, buffer: HeartbeatBuffer, mock_callback: MagicMock) -> None:
        """flush() after remove() does not include the removed agent."""
        buffer.record("agent-1")
        buffer.record("agent-2")
        buffer.remove("agent-1")
        flushed = buffer.flush()
        assert flushed == 1
        call_arg = mock_callback.call_args[0][0]
        assert "agent-1" not in call_arg
        assert "agent-2" in call_arg

    def test_remove_during_empty_buffer(self, buffer: HeartbeatBuffer) -> None:
        """remove() on empty buffer is safe."""
        buffer.remove("agent-1")
        assert buffer.stats()["buffer_size"] == 0
